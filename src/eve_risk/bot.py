from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta

import botpy
import httpx
import uvicorn
from redis.asyncio import Redis

from eve_risk.admission import AdmissionController, AdmissionResult
from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.config import get_settings
from eve_risk.domain import AnalysisRequest
from eve_risk.health import app as health_app
from eve_risk.parser import RosterParseError, is_help_command, parse_roster
from eve_risk.queueing import AnalysisQueue

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "EVE 敌对舰队分析（Tranquility）\n"
    "用法：@机器人 分析，然后每行一个角色名；也支持逗号或分号分隔。\n"
    "一次最多 30 人，默认分析近 90 天公开战报。"
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

        if is_help_command(content):
            await self.qq.send_text(group_openid, msg_id, HELP_TEXT, msg_seq=1)
            return
        try:
            names = parse_roster(content, self.settings.max_characters)
        except RosterParseError as exc:
            await self.qq.send_text(group_openid, msg_id, str(exc), msg_seq=1)
            return

        now = datetime.now(UTC)
        request_id = str(uuid.uuid4())
        request = AnalysisRequest(
            request_id=request_id,
            msg_id=msg_id,
            group_openid=group_openid,
            member_openid=member_openid,
            character_names=names,
            received_at=now,
            fetch_deadline_at=now
            + timedelta(seconds=self.settings.analysis_fetch_deadline_seconds),
            reply_deadline_at=now
            + timedelta(seconds=self.settings.analysis_reply_deadline_seconds),
        )
        result = await self.admission.admit(
            job_id=request_id,
            msg_id=msg_id,
            member_openid=member_openid,
            group_openid=group_openid,
            now_epoch=int(now.timestamp()),
            deadline_epoch=int(request.reply_deadline_at.timestamp()),
        )
        if result == AdmissionResult.DUPLICATE:
            return
        if result != AdmissionResult.OK:
            await self.qq.send_text(group_openid, msg_id, ADMISSION_MESSAGES[result], msg_seq=1)
            return

        try:
            await self.queue.enqueue(request)
        except Exception:
            await self.admission.release(request_id, group_openid)
            logger.exception("request_id=%s enqueue_failed", request_id)
            await self.qq.send_text(group_openid, msg_id, "任务创建失败，请稍后重试。", msg_seq=1)
            return

        await self.qq.send_text(
            group_openid,
            msg_id,
            f"已受理 {len(names)} 个角色，正在抓取近 90 天公开战报。",
            msg_seq=1,
        )
        logger.info("request_id=%s admitted characters=%d", request_id, len(names))


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
