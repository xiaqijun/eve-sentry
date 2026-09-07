from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta

import botpy
import httpx
import uvicorn
from redis.asyncio import Redis

from eve_risk.admission import AdmissionController, AdmissionResult
from eve_risk.alerts import EveSentryAlertRelay, alert_subscription_action
from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.config import get_settings
from eve_risk.domain import AnalysisRequest
from eve_risk.health import app as health_app
from eve_risk.parser import (
    RosterParseError,
    is_analysis_command,
    is_help_command,
    parse_roster,
)
from eve_risk.queueing import AnalysisQueue
from eve_risk.sentry_status import (
    EveSentryStatusClient,
    SentryStatusError,
    format_ocr_query,
    format_query_menu,
    parse_sentry_query,
)
from eve_risk.sentry_watch import (
    WATCH_TYPE_LABELS,
    SentryWatchMonitor,
    SentryWatchRepository,
    format_watch_help,
    format_watch_list,
    parse_watch_command,
    watch_short_id,
)
from eve_risk.server_status import EveServerStartupMonitor
from eve_risk.storage import create_session_factory

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "EVE 敌对舰队分析（Tranquility）\n"
    "用法：@机器人 分析可分析当前预警中的已确认人员；也可在分析后输入角色名。\n"
    "多人会分别生成报告，角色名支持换行、逗号或分号分隔。\n"
    "一次最多 30 人，默认分析近 90 天公开战报。\n"
    "预警：@机器人 开启预警 / 关闭预警 / 预警状态。\n"
    "查询菜单：@机器人 查询。\n"
    "查询：查询星系 名称 / 查询节点敌情 / 查询所有节点 / 查询预警节点。\n"
    "定向查询：查询人员 名称 / 查询军团 名称 / 查询联盟 名称。\n"
    "上线监测：上线监测 人员/军团/联盟 名称 间隔（最低 30 秒）。"
)

ADMISSION_MESSAGES = {
    AdmissionResult.MEMBER_RATE: "请求过于频繁，同一成员 60 秒内只能提交一次。",
    AdmissionResult.GROUP_BUSY: "本群已有分析任务，请等待当前任务完成。",
    AdmissionResult.GLOBAL_BUSY: "机器人当前任务较多，请稍后重试。",
}


class RiskBotClient(botpy.Client):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        settings = get_settings()
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url)
        self.http_client = httpx.AsyncClient(headers={"Accept": "application/json"})
        self.qq = QQOpenAPIClient(
            self.http_client,
            self.redis,
            settings.qq_app_id,
            settings.qq_app_secret,
            settings.qq_token_url,
            settings.qq_api_base_url,
        )
        self.admission = AdmissionController(
            self.redis,
            context_ttl=settings.qq_context_ttl_seconds,
            member_ttl=settings.member_rate_limit_seconds,
            group_ttl=settings.group_job_ttl_seconds,
            max_jobs=settings.global_max_jobs,
        )
        self.queue = AnalysisQueue(settings.redis_url)
        self.sentry_status = EveSentryStatusClient(
            self.http_client,
            settings.eve_sentry_events_url,
            settings.eve_sentry_api_key,
        )
        self.watch_engine, watch_sessions = create_session_factory(settings.database_url)
        self.watch_repository = SentryWatchRepository(watch_sessions)
        self.watch_monitor = SentryWatchMonitor(
            self.watch_repository,
            self.sentry_status,
            self.qq,
            self.redis,
            poll_seconds=settings.eve_sentry_watch_poll_seconds,
            query_timeout_seconds=settings.eve_sentry_query_soft_timeout_seconds,
            snapshot_reuse_seconds=(
                settings.eve_sentry_watch_snapshot_reuse_seconds
            ),
        )
        self.watch_task: asyncio.Task[None] | None = None
        self.query_tasks: set[asyncio.Task[None]] = set()
        self.alert_relay = EveSentryAlertRelay(
            self.http_client,
            self.redis,
            self.qq,
            settings.eve_sentry_events_url,
            api_key=settings.eve_sentry_api_key,
            min_level=settings.eve_sentry_alert_min_level,
            public_url=settings.eve_sentry_public_url,
            personnel_push_interval_seconds=(
                settings.eve_sentry_personnel_push_interval_seconds
            ),
        )
        self.alert_task: asyncio.Task[None] | None = None
        status_url = settings.eve_server_status_url if settings.eve_server_status_enabled else ""
        self.server_status_monitor = EveServerStartupMonitor(
            self.http_client,
            self.redis,
            self.qq,
            status_url,
            poll_interval_seconds=settings.eve_server_status_poll_seconds,
            offline_threshold=settings.eve_server_offline_threshold,
        )
        self.server_status_task: asyncio.Task[None] | None = None

    async def on_ready(self) -> None:
        if not self.alert_relay.enabled:
            logger.info("EVE Sentry proactive alerts are disabled")
        elif self.alert_task is None or self.alert_task.done():
            self.alert_task = asyncio.create_task(
                self.alert_relay.run_forever(),
                name="eve-sentry-alert-relay",
            )
            logger.info("EVE Sentry proactive alert relay started")
        if self.server_status_monitor.enabled and (
            self.server_status_task is None or self.server_status_task.done()
        ):
            self.server_status_task = asyncio.create_task(
                self.server_status_monitor.run_forever(),
                name="eve-server-startup-monitor",
            )
            logger.info("EVE server startup monitor started")
        if self.sentry_status.enabled and (
            self.watch_task is None or self.watch_task.done()
        ):
            self.watch_task = asyncio.create_task(
                self.watch_monitor.run_forever(),
                name="eve-sentry-online-watch",
            )
            logger.info("EVE Sentry online watch monitor started")

    async def on_group_at_message_create(self, message: object) -> None:
        msg_id = str(getattr(message, "id", ""))
        group_openid = str(getattr(message, "group_openid", ""))
        content = str(getattr(message, "content", ""))
        author = getattr(message, "author", None)
        member_openid = str(
            getattr(author, "member_openid", "")
            or (author.get("member_openid", "") if isinstance(author, dict) else "")
        )
        if not all((msg_id, group_openid, member_openid)):
            logger.warning("Ignored malformed QQ group event")
            return
        first_delivery = await self.redis.set(
            f"qq:event:{msg_id}", "1", ex=self.settings.qq_context_ttl_seconds, nx=True
        )
        if not first_delivery:
            return

        subscription_action = alert_subscription_action(content)
        if subscription_action:
            if not self.alert_relay.enabled:
                await self.qq.send_text(
                    group_openid,
                    msg_id,
                    "主动预警尚未配置，请联系机器人管理员。",
                    msg_seq=1,
                )
                return
            if subscription_action == "enable":
                await self.alert_relay.subscribe(group_openid)
                reply = "已开启 EVE Sentry 主动预警，新敌对告警会推送到本群。"
            elif subscription_action == "disable":
                await self.alert_relay.unsubscribe(group_openid)
                reply = "已关闭本群的 EVE Sentry 主动预警。"
            else:
                subscribed = await self.alert_relay.is_subscribed(group_openid)
                reply = f"本群主动预警：{'已开启' if subscribed else '未开启'}。"
            await self.qq.send_text(group_openid, msg_id, reply, msg_seq=1)
            return

        if is_help_command(content):
            await self.qq.send_text(group_openid, msg_id, HELP_TEXT, msg_seq=1)
            return
        watch_command = parse_watch_command(
            content,
            default_interval=self.settings.eve_sentry_watch_default_interval_seconds,
            minimum_interval=max(
                30,
                self.settings.eve_sentry_watch_min_interval_seconds,
            ),
        )
        if watch_command is not None:
            await self._handle_watch_command(
                watch_command,
                group_openid=group_openid,
                member_openid=member_openid,
                msg_id=msg_id,
            )
            return
        sentry_query = parse_sentry_query(content)
        if sentry_query is not None:
            if sentry_query.get("mode") == "menu":
                await self._send_query_markdown(
                    group_openid,
                    format_query_menu(),
                    msg_id=msg_id,
                )
                return
            if sentry_query.get("mode") in {"system_roster", "filtered", "all_nodes"}:
                await self._start_sentry_ocr_query(
                    sentry_query,
                    group_openid=group_openid,
                    msg_id=msg_id,
                )
                return
            try:
                reply = await self.sentry_status.query(sentry_query)
            except SentryStatusError as exc:
                await self.qq.send_text(
                    group_openid, msg_id, str(exc), msg_seq=1
                )
                return
            try:
                await self.qq.send_proactive_markdown(group_openid, reply)
            except Exception:
                logger.warning(
                    "QQ sentry query markdown delivery failed; falling back to text"
                )
                await self.qq.send_text(group_openid, msg_id, reply, msg_seq=1)
            return
        if is_analysis_command(content):
            names = await self.alert_relay.current_analysis_names(
                self.settings.max_characters
            )
            if not names:
                await self.qq.send_text(
                    group_openid,
                    msg_id,
                    "当前没有已确认的敌对人员可供分析。",
                    msg_seq=1,
                )
                return
        else:
            try:
                names = parse_roster(content, self.settings.max_characters)
            except RosterParseError as exc:
                await self.qq.send_text(group_openid, msg_id, str(exc), msg_seq=1)
                return

        batch_id = f"message:{msg_id}" if len(names) > 1 else None
        admitted = 0
        first_failure: AdmissionResult | None = None
        enqueue_failed = False
        for index, name in enumerate(names):
            now = datetime.now(UTC)
            request_id = str(uuid.uuid4())
            request = AnalysisRequest(
                request_id=request_id,
                msg_id=msg_id,
                group_openid=group_openid,
                member_openid=member_openid,
                character_names=[name],
                received_at=now,
                fetch_deadline_at=now
                + timedelta(seconds=self.settings.analysis_fetch_deadline_seconds),
                reply_deadline_at=now
                + timedelta(seconds=self.settings.analysis_reply_deadline_seconds),
                reply_seq=index + 1,
                admission_batch_id=batch_id,
            )
            if batch_id is None:
                result = await self.admission.admit(
                    job_id=request_id,
                    msg_id=msg_id,
                    member_openid=member_openid,
                    group_openid=group_openid,
                    now_epoch=int(now.timestamp()),
                    deadline_epoch=int(request.reply_deadline_at.timestamp()),
                )
            else:
                result = await self.admission.admit_batch(
                    job_id=request_id,
                    msg_id=f"{msg_id}:{index}",
                    member_openid=member_openid,
                    group_openid=group_openid,
                    batch_id=batch_id,
                    now_epoch=int(now.timestamp()),
                    deadline_epoch=int(request.reply_deadline_at.timestamp()),
                )
            if result == AdmissionResult.DUPLICATE:
                continue
            if result != AdmissionResult.OK:
                first_failure = first_failure or result
                break

            try:
                await self.queue.enqueue(request)
            except Exception:
                enqueue_failed = True
                if request.admission_batch_id:
                    await self.admission.release(
                        request_id, group_openid, request.admission_batch_id
                    )
                else:
                    await self.admission.release(request_id, group_openid)
                logger.exception("request_id=%s enqueue_failed", request_id)
                break
            admitted += 1
            logger.info(
                "request_id=%s admitted character=%s batch=%s",
                request_id,
                name,
                batch_id or "none",
            )

        if admitted:
            if first_failure is not None or enqueue_failed:
                logger.warning(
                    "analysis batch partially admitted group=%s admitted=%d total=%d",
                    group_openid,
                    admitted,
                    len(names),
                )
            return
        if first_failure is not None:
            await self.qq.send_text(
                group_openid,
                msg_id,
                ADMISSION_MESSAGES[first_failure],
                msg_seq=1,
            )
            return
        if enqueue_failed:
            await self.qq.send_text(
                group_openid,
                msg_id,
                "任务创建失败，请稍后重试。",
                msg_seq=1,
            )

    async def _send_query_markdown(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str = "",
    ) -> None:
        keyboard_id = self.settings.qq_query_keyboard_id
        try:
            if msg_id:
                await self.qq.send_markdown(
                    group_openid,
                    msg_id,
                    content,
                    1,
                    keyboard_id=keyboard_id,
                )
            else:
                await self.qq.send_proactive_markdown(
                    group_openid,
                    content,
                    keyboard_id=keyboard_id,
                )
        except Exception:
            logger.warning("QQ query markdown delivery failed; falling back to text")
            if msg_id:
                await self.qq.send_text(group_openid, msg_id, content, msg_seq=1)
            else:
                await self.qq.send_proactive_text(group_openid, content)

    async def _start_sentry_ocr_query(
        self,
        query: dict[str, str],
        *,
        group_openid: str,
        msg_id: str,
    ) -> None:
        group_hash = hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:16]
        lock_key = f"qq:ocr-query:group:{group_hash}"
        token = uuid.uuid4().hex
        locked = await self.redis.set(
            lock_key,
            token,
            ex=self.settings.eve_sentry_query_lock_seconds,
            nx=True,
        )
        if not locked:
            await self.qq.send_text(
                group_openid,
                msg_id,
                "本群已有 OCR 查询正在进行，请等待当前结果。",
                msg_seq=1,
            )
            return
        try:
            created = await self.sentry_status.create_ocr_query(query)
        except SentryStatusError as exc:
            await self.redis.delete(lock_key)
            await self.qq.send_text(group_openid, msg_id, str(exc), msg_seq=1)
            return
        requested = created.get("requested_clients")
        requested_count = len(requested) if isinstance(requested, list) else 0
        await self.qq.send_text(
            group_openid,
            msg_id,
            f"OCR 查询任务已下发｜目标节点 {requested_count}",
            msg_seq=1,
        )
        task = asyncio.create_task(
            self._finish_sentry_ocr_query(
                query,
                created,
                group_openid=group_openid,
                lock_key=lock_key,
                lock_token=token,
            ),
            name=f"eve-sentry-ocr-query-{created.get('query_id', 'unknown')}",
        )
        self.query_tasks.add(task)
        task.add_done_callback(self.query_tasks.discard)

    async def _finish_sentry_ocr_query(
        self,
        query: dict[str, str],
        created: dict[str, object],
        *,
        group_openid: str,
        lock_key: str,
        lock_token: str,
    ) -> None:
        query_id = str(created.get("query_id") or "").strip()
        try:
            payload = await self.sentry_status.wait_ocr_query_payload(
                created,
                timeout_seconds=self.settings.eve_sentry_query_soft_timeout_seconds,
            )
            delivered_key = f"qq:ocr-query:delivered:{query_id}"
            first_delivery = await self.redis.set(
                delivered_key,
                "1",
                ex=self.settings.qq_context_ttl_seconds,
                nx=True,
            )
            if first_delivery:
                await self._send_query_markdown(
                    group_openid,
                    format_ocr_query(payload, query),
                )
        except SentryStatusError as exc:
            await self.qq.send_proactive_text(group_openid, str(exc))
        except Exception:
            logger.exception("QQ OCR query completion failed query_id=%s", query_id)
            await self.qq.send_proactive_text(
                group_openid,
                "OCR 查询结果发送失败，请稍后重试。",
            )
        finally:
            current = await self.redis.get(lock_key)
            if current in {lock_token, lock_token.encode()}:
                await self.redis.delete(lock_key)

    async def _handle_watch_command(
        self,
        command: object,
        *,
        group_openid: str,
        member_openid: str,
        msg_id: str,
    ) -> None:
        action = str(getattr(command, "action", ""))
        if action in {"help", "delete_help"}:
            await self._send_query_markdown(
                group_openid,
                format_watch_help(self.settings.eve_sentry_watch_min_interval_seconds),
                msg_id=msg_id,
            )
            return
        if action == "interval_too_short":
            await self.qq.send_text(
                group_openid,
                msg_id,
                f"监测间隔不能小于 {self.settings.eve_sentry_watch_min_interval_seconds} 秒。",
                msg_seq=1,
            )
            return
        if action == "list":
            records = await self.watch_repository.list_group(group_openid)
            await self._send_query_markdown(
                group_openid,
                format_watch_list(records),
                msg_id=msg_id,
            )
            return
        if action == "delete_all":
            deleted = await self.watch_repository.delete_all(group_openid)
            await self.qq.send_text(
                group_openid,
                msg_id,
                f"已取消本群全部上线监测｜{deleted} 条",
                msg_seq=1,
            )
            return
        if action == "delete":
            deleted = await self.watch_repository.delete_one(
                group_openid,
                watch_id=str(getattr(command, "watch_id", "")),
                target_type=str(getattr(command, "target_type", "")),
                target_value=str(getattr(command, "target_value", "")),
            )
            if deleted is None:
                reply = "没有找到对应的上线监测任务。"
            else:
                reply = (
                    f"已取消上线监测｜{watch_short_id(deleted.watch_id)}｜"
                    f"{WATCH_TYPE_LABELS.get(deleted.target_type, deleted.target_type)} "
                    f"{deleted.target_value}"
                )
            await self.qq.send_text(group_openid, msg_id, reply, msg_seq=1)
            return
        record = await self.watch_repository.add(
            group_openid,
            member_openid,
            str(getattr(command, "target_type", "")),
            str(getattr(command, "target_value", "")),
            int(getattr(command, "interval_seconds", 0)),
            datetime.now(UTC),
        )
        await self.qq.send_text(
            group_openid,
            msg_id,
            f"已添加上线监测｜{watch_short_id(record.watch_id)}｜"
            f"{WATCH_TYPE_LABELS.get(record.target_type, record.target_type)} "
            f"{record.target_value}｜每 {record.interval_seconds} 秒",
            msg_seq=1,
        )


def _start_health_server() -> None:
    settings = get_settings()
    uvicorn.run(
        health_app,
        host=settings.health_host,
        port=settings.health_port,
        log_level=settings.log_level.lower(),
    )


def main() -> None:
    settings = get_settings()
    settings.require_qq()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botpy").setLevel(logging.WARNING)
    threading.Thread(target=_start_health_server, daemon=True).start()
    intents = botpy.Intents(public_messages=True)
    client = RiskBotClient(intents=intents, bot_log=False)
    client.run(appid=settings.qq_app_id, secret=settings.qq_app_secret)


if __name__ == "__main__":
    main()
