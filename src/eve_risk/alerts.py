from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
from redis.asyncio import Redis

from eve_risk.clients.qq import QQOpenAPIClient

logger = logging.getLogger(__name__)

ALERT_GROUPS_KEY = "qq:eve-sentry:alert-groups"
ALERT_CURSOR_KEY = "qq:eve-sentry:alert-cursor"
ALERT_DELIVERED_PREFIX = "qq:eve-sentry:alert-delivered"
ACTIVE_INTEL_STATE_KEY = "qq:eve-sentry:active-intel-state"
SYSTEM_ALERT_STATE_KEY = "qq:eve-sentry:system-alert-state"
SYSTEM_ALERT_STATE_READY_KEY = "qq:eve-sentry:system-alert-state-ready"
ALERT_DEDUPE_SECONDS = 7 * 24 * 60 * 60

_ENABLE_COMMANDS = {"开启预警", "订阅预警", "打开预警"}
_DISABLE_COMMANDS = {"关闭预警", "取消预警", "停止预警"}
_STATUS_COMMANDS = {"预警状态"}
_LEVEL_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "critical": "严重",
}
_LEVEL_RANKS = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_SOURCE_LABELS = {
    "eve-sentry-detector": "OCR 监控",
    "local_ocr": "OCR 监控",
    "ocr": "OCR 监控",
    "intel_channel": "预警频道",
}


def alert_subscription_action(content: str) -> str | None:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"^\s*<@!?\w+>\s*", "", normalized, count=1).strip()
    if normalized in _ENABLE_COMMANDS:
        return "enable"
    if normalized in _DISABLE_COMMANDS:
        return "disable"
    if normalized in _STATUS_COMMANDS:
        return "status"
    return None


def format_active_intel_message(
    item: dict[str, Any],
    transition: str,
    occurred_at: str,
    public_url: str = "",
) -> str:
    entered = transition == "entered"
    title = "### 🔴 敌对进入" if entered else "### 🟢 敌对离开"
    remaining_count = item.get("_remaining_count")
    if isinstance(remaining_count, int) and remaining_count >= 0:
        title = f"{title}｜当前敌对 {remaining_count} 人"
    lines = [
        title,
        f"**位置**｜{_system_label(item)}",
        f"**目标**｜{_target_label(item)}",
    ]
    alliance = _affiliation_label(item, "alliance")
    if alliance:
        lines.append(f"**联盟**｜{alliance}")
    corporation = _affiliation_label(item, "corporation")
    if corporation:
        lines.append(f"**军团**｜{corporation}")
    if entered:
        threat = _threat_label(item)
        if threat:
            lines.append(f"**威胁**｜{threat}")
    lines.append(
        f"**{'进入时间' if entered else '离开时间'}**｜{_format_alert_time(occurred_at)}"
    )
    if not entered:
        duration = _format_duration(str(item.get("first_seen_at") or ""), occurred_at)
        if duration:
            duration_label = "最长停留" if int(item.get("_grouped_count") or 1) > 1 else "停留"
            lines.append(f"**{duration_label}**｜{duration}")
    return "\n".join(lines)


def format_alert_message(alert: dict[str, Any], public_url: str = "") -> str:
    """Backward-compatible formatter for a newly entered active target."""
    occurred_at = str(
        alert.get("first_seen_at") or alert.get("created_at") or datetime.now(UTC).isoformat()
    )
    return format_active_intel_message(alert, "entered", occurred_at, public_url)


def format_system_alert_message(system_name: str, transition: str) -> str:
    system_name = str(system_name or "").strip() or "未知星系"
    if transition == "safe":
        return f"✅ {system_name} 清空"
    return f"❗ {system_name} 来敌"


def format_monitoring_node_message(change: dict[str, Any]) -> str:
    change_type = str(change.get("change") or "").strip().casefold()
    account = str(
        change.get("character_name")
        or change.get("source_instance")
        or change.get("client_id")
        or "未知账号"
    ).strip()
    system_name = str(change.get("system_name") or "Unknown").strip() or "Unknown"
    if change_type == "online":
        return f"🟢 监控节点上线\n账号｜{account}\n位置｜{system_name}"
    if change_type == "offline":
        return f"⚪ 监控节点下线\n账号｜{account}\n最后位置｜{system_name}"
    from_system = str(change.get("from_system") or "Unknown").strip() or "Unknown"
    to_system = str(change.get("to_system") or system_name).strip() or "Unknown"
    return f"🔵 监控节点移动\n账号｜{account}\n位置｜{from_system} → {to_system}"


async def iter_sse_events(
    lines: AsyncIterable[str],
) -> AsyncIterator[tuple[str, str, str]]:
    event_name = "message"
    event_id = ""
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, event_id, "\n".join(data_lines)
            event_name = "message"
            event_id = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, event_id, "\n".join(data_lines)


class EveSentryAlertRelay:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        qq: QQOpenAPIClient,
        events_url: str,
        *,
        api_key: str = "",
        min_level: str = "",
        public_url: str = "",
        reconnect_delay_seconds: float = 3.0,
    ) -> None:
        self.http = http
        self.redis = redis
        self.qq = qq
        self.events_url = events_url.strip()
        self.api_key = api_key.strip()
        self.min_level = min_level.strip().casefold()
        self.public_url = public_url.strip()
        self.reconnect_delay_seconds = max(0.1, float(reconnect_delay_seconds))

    @property
    def enabled(self) -> bool:
        return bool(self.events_url)

    async def subscribe(self, group_openid: str) -> None:
        await self.redis.sadd(ALERT_GROUPS_KEY, group_openid)

    async def unsubscribe(self, group_openid: str) -> None:
        await self.redis.srem(ALERT_GROUPS_KEY, group_openid)

    async def is_subscribed(self, group_openid: str) -> bool:
        return bool(await self.redis.sismember(ALERT_GROUPS_KEY, group_openid))

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EVE Sentry alert stream disconnected")
            await asyncio.sleep(self.reconnect_delay_seconds)

    async def deliver(
        self,
        item: dict[str, Any],
        transition: str = "entered",
        occurred_at: str = "",
    ) -> None:
        active_id = str(item.get("id") or "").strip()
        occurred_at = str(
            occurred_at
            or item.get("first_seen_at")
            or item.get("created_at")
        ).strip()
        if not active_id or not occurred_at:
            logger.warning("Ignored malformed EVE Sentry active intel transition")
            return

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_active_intel_message(
            item,
            transition,
            occurred_at,
            self.public_url,
        )
        event_id = f"{transition}:{active_id}:{occurred_at}"
        delivered = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                send_markdown = getattr(self.qq, "send_proactive_markdown", None)
                if send_markdown is None:
                    await self.qq.send_proactive_text(
                        group_openid, _markdown_to_plain_text(message)
                    )
                else:
                    try:
                        await send_markdown(group_openid, message)
                    except Exception:
                        logger.warning(
                            "QQ proactive markdown delivery failed; falling back to text"
                        )
                        await self.qq.send_proactive_text(
                            group_openid, _markdown_to_plain_text(message)
                        )
            except Exception:
                logger.exception("QQ proactive alert delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        logger.info(
            "EVE Sentry active intel transition processed transition=%s deliveries=%d",
            transition,
            delivered,
        )

    async def deliver_system_transition(
        self,
        state: dict[str, Any],
        transition: str,
    ) -> bool:
        system_name = _system_label(state)
        episode_id = str(state.get("episode_id") or "").strip()
        if not episode_id:
            logger.warning("Ignored malformed EVE Sentry system transition")
            return True

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_system_alert_message(system_name, transition)
        event_id = f"system:{transition}:{system_name.casefold()}:{episode_id}"
        delivered = 0
        failed = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                failed += 1
                logger.exception("QQ proactive system alert delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        logger.info(
            "EVE Sentry system transition processed transition=%s deliveries=%d failures=%d",
            transition,
            delivered,
            failed,
        )
        return failed == 0

    async def deliver_monitoring_node_change(
        self,
        change: dict[str, Any],
        occurred_at: str,
    ) -> None:
        change_type = str(change.get("change") or "").strip().casefold()
        if change_type not in {"online", "offline", "moved"}:
            logger.warning("Ignored unknown EVE Sentry monitoring node change")
            return
        node_id = str(
            change.get("node_id")
            or change.get("client_id")
            or change.get("source_instance")
            or change.get("character_name")
            or ""
        ).strip()
        if not node_id:
            logger.warning("Ignored monitoring node change without identity")
            return

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_monitoring_node_message(change)
        event_id = ":".join(
            (
                "node",
                change_type,
                node_id,
                str(change.get("from_system") or ""),
                str(change.get("to_system") or change.get("system_name") or ""),
                occurred_at,
            )
        )
        delivered = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                logger.exception("QQ monitoring node delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        logger.info(
            "EVE Sentry monitoring node change processed change=%s deliveries=%d",
            change_type,
            delivered,
        )

    async def process_monitoring_node(self, payload: dict[str, Any]) -> None:
        changes = payload.get("changes")
        if not isinstance(changes, list):
            logger.warning("Ignored EVE Sentry monitoring node event without changes")
            return
        occurred_at = str(
            payload.get("generated_at") or datetime.now(UTC).isoformat()
        ).strip()
        for change in changes:
            if isinstance(change, dict):
                await self.deliver_monitoring_node_change(change, occurred_at)

    async def process_bootstrap(self, payload: dict[str, Any]) -> None:
        node_changes = payload.get("monitoring_node_changes")
        if isinstance(node_changes, list) and node_changes:
            await self.process_monitoring_node(
                {
                    "generated_at": payload.get("generated_at"),
                    "changes": node_changes,
                }
            )
        active_intel = payload.get("active_intel")
        if not isinstance(active_intel, list):
            logger.warning("Ignored EVE Sentry bootstrap without active_intel list")
            return

        generated_at = str(payload.get("generated_at") or datetime.now(UTC).isoformat())
        active_items = {
            active_id: item
            for active_id, item in _active_intel_map(
                active_intel, payload.get("alerts")
            ).items()
            if self._allows_transition(item)
        }
        current = _active_system_state(active_items.values(), generated_at)
        previous, initialized = await self._load_system_alert_state()

        for system_key in current.keys() & previous.keys():
            current[system_key]["episode_id"] = previous[system_key]["episode_id"]

        transitions_succeeded = True
        if initialized:
            for system_key in sorted(previous.keys() - current.keys()):
                transitions_succeeded = (
                    await self.deliver_system_transition(previous[system_key], "safe")
                    and transitions_succeeded
                )
            for system_key in sorted(current.keys() - previous.keys()):
                transitions_succeeded = (
                    await self.deliver_system_transition(current[system_key], "alert")
                    and transitions_succeeded
                )

        if not transitions_succeeded:
            logger.warning("EVE Sentry system state update deferred after delivery failure")
            return

        await self._save_system_alert_state(current)
        await self.redis.set(ALERT_CURSOR_KEY, generated_at)
        logger.info(
            "EVE Sentry system alerts synchronized systems=%d hostiles=%d initialized=%s",
            len(current),
            len(active_items),
            initialized,
        )

    async def _load_system_alert_state(
        self,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        ready, raw_items = await asyncio.gather(
            self.redis.exists(SYSTEM_ALERT_STATE_READY_KEY),
            self.redis.hgetall(SYSTEM_ALERT_STATE_KEY),
        )
        result: dict[str, dict[str, Any]] = {}
        for raw_key, raw_payload in raw_items.items():
            system_key = _decode(raw_key)
            try:
                item = json.loads(_decode(raw_payload))
            except json.JSONDecodeError:
                continue
            if system_key and isinstance(item, dict):
                result[system_key] = item
        return result, bool(ready)

    async def _save_system_alert_state(
        self, items: dict[str, dict[str, Any]]
    ) -> None:
        pipeline = self.redis.pipeline()
        pipeline.delete(SYSTEM_ALERT_STATE_KEY)
        if items:
            pipeline.hset(
                SYSTEM_ALERT_STATE_KEY,
                mapping={
                    system_key: json.dumps(
                        item, ensure_ascii=False, separators=(",", ":")
                    )
                    for system_key, item in items.items()
                },
            )
        pipeline.set(SYSTEM_ALERT_STATE_READY_KEY, "1")
        await pipeline.execute()

    async def _load_active_intel_state(self) -> dict[str, dict[str, Any]]:
        raw_items = await self.redis.hgetall(ACTIVE_INTEL_STATE_KEY)
        result: dict[str, dict[str, Any]] = {}
        for raw_id, raw_payload in raw_items.items():
            active_id = _decode(raw_id)
            try:
                item = json.loads(_decode(raw_payload))
            except json.JSONDecodeError:
                continue
            if active_id and isinstance(item, dict):
                result[active_id] = item
        return result

    async def _save_active_intel_state(
        self, items: dict[str, dict[str, Any]]
    ) -> None:
        pipeline = self.redis.pipeline()
        pipeline.delete(ACTIVE_INTEL_STATE_KEY)
        if items:
            pipeline.hset(
                ACTIVE_INTEL_STATE_KEY,
                mapping={
                    active_id: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    for active_id, item in items.items()
                },
            )
        await pipeline.execute()

    def _allows_transition(self, item: dict[str, Any]) -> bool:
        if not self.min_level:
            return True
        level = str(item.get("level") or "").strip().casefold()
        if not level:
            return True
        return _LEVEL_RANKS.get(level, 0) >= _LEVEL_RANKS.get(self.min_level, 0)

    async def _stream_once(self) -> None:
        cursor = _decode(await self.redis.get(ALERT_CURSOR_KEY))
        params = {
            "limit": "50",
            "timeout": "30",
            "heartbeat": "15",
            "bootstrap": "1",
            "since": cursor or datetime.now(UTC).isoformat(),
        }
        if self.min_level:
            params["min_level"] = self.min_level
        timeout = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)
        async with self.http.stream(
            "GET",
            self.events_url,
            params=params,
            headers={
                "Accept": "text/event-stream",
                **(
                    {"Authorization": f"Bearer {self.api_key}"}
                    if self.api_key
                    else {}
                ),
            },
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for event_name, _event_id, data in iter_sse_events(
                response.aiter_lines()
            ):
                payload = json.loads(data)
                if event_name == "bootstrap" and isinstance(payload, dict):
                    await self.process_bootstrap(payload)
                elif event_name == "monitoring_node" and isinstance(payload, dict):
                    await self.process_monitoring_node(payload)


def _active_intel_map(
    raw_items: list[object], raw_alerts: object
) -> dict[str, dict[str, Any]]:
    alerts_by_active_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_alerts, list):
        for raw_alert in raw_alerts:
            if not isinstance(raw_alert, dict):
                continue
            active_id = str(raw_alert.get("active_intel_id") or "").strip()
            if active_id and active_id not in alerts_by_active_id:
                alerts_by_active_id[active_id] = raw_alert

    result: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or raw_item.get("active") is False:
            continue
        active_id = str(raw_item.get("id") or "").strip()
        alert = alerts_by_active_id.get(active_id)
        if not active_id or alert is None:
            continue
        item = dict(raw_item)
        for key in ("level", "score", "classification", "names"):
            if item.get(key) in (None, "", []) and alert.get(key) not in (
                None,
                "",
                [],
            ):
                item[key] = alert[key]
        result[active_id] = item
    return result


def _active_system_state(
    items: Iterable[dict[str, Any]], fallback_episode_id: str
) -> dict[str, dict[str, Any]]:
    systems: dict[str, dict[str, Any]] = {}
    for item in items:
        system_name = _system_label(item)
        system_key = system_name.casefold()
        state = systems.setdefault(
            system_key,
            {
                "system_name": system_name,
                "hostile_count": 0,
                "episode_id": fallback_episode_id,
            },
        )
        state["hostile_count"] += 1
    return systems


def _transition_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(_transition_group_key(item), []).append(item)
    return [_combine_transition_items(group) for group in grouped.values()]


def _transition_group_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _system_label(item).casefold(),
        str(item.get("source") or "").strip().casefold(),
        str(item.get("source_instance") or "").strip().casefold(),
    )


def _combine_transition_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    if len(items) == 1:
        return items[0]
    ordered = sorted(items, key=lambda item: str(item.get("id") or ""))
    combined = dict(ordered[0])
    active_ids = [str(item.get("id") or "") for item in ordered]
    digest = hashlib.sha256("\0".join(active_ids).encode("utf-8")).hexdigest()[:20]
    combined["id"] = f"batch:{digest}"
    combined["_grouped_count"] = len(ordered)
    names = [str(item.get("name") or "").strip() for item in ordered]
    combined["names"] = [name for name in names if name]
    first_seen_values = [
        str(item.get("first_seen_at") or "").strip()
        for item in ordered
        if str(item.get("first_seen_at") or "").strip()
    ]
    if first_seen_values:
        combined["first_seen_at"] = min(first_seen_values)

    levels = [
        str(item.get("level") or "").strip().casefold()
        for item in ordered
        if str(item.get("level") or "").strip()
    ]
    if levels:
        combined["level"] = max(levels, key=lambda level: _LEVEL_RANKS.get(level, 0))
    scores = [item.get("score") for item in ordered]
    numeric_scores = [score for score in scores if isinstance(score, int | float)]
    if numeric_scores:
        combined["score"] = max(numeric_scores)

    metadata = dict(_metadata(combined))
    for key in (
        "alliance_ticker",
        "alliance_name",
        "corporation_ticker",
        "corporation_name",
    ):
        values = {str(_metadata(item).get(key) or "").strip() for item in ordered}
        if len(values) > 1:
            metadata.pop(key, None)
    combined["metadata"] = metadata
    return combined


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _system_label(item: dict[str, Any]) -> str:
    return str(item.get("system_name") or "未知星系").strip() or "未知星系"


def _target_label(item: dict[str, Any]) -> str:
    raw_names = item.get("names")
    if isinstance(raw_names, list):
        names = [str(name).strip() for name in raw_names if str(name).strip()]
        if names:
            return "、".join(names[:5]) + (f" 等 {len(names)} 人" if len(names) > 5 else "")

    name = str(item.get("name") or "").strip()
    if name:
        return name

    metadata = _metadata(item)
    hostile_count = _positive_int(
        metadata.get("hostile_count")
        or item.get("hostile_count")
        or item.get("target_count")
    )
    raw_text = str(item.get("raw_text") or "").strip()
    if raw_text:
        return raw_text if len(raw_text) <= 80 else raw_text[:77] + "..."
    if hostile_count is not None:
        return f"{hostile_count} 名敌对"
    return str(item.get("display_name") or "未知目标").strip() or "未知目标"


def _affiliation_label(item: dict[str, Any], kind: str) -> str:
    metadata = _metadata(item)
    return _named_affiliation(
        metadata.get(f"{kind}_ticker"), metadata.get(f"{kind}_name")
    )


def _named_affiliation(ticker: object, name: object) -> str:
    ticker_text = str(ticker or "").strip()
    name_text = str(name or "").strip()
    if ticker_text and name_text:
        return f"[{ticker_text}] {name_text}"
    return name_text or (f"[{ticker_text}]" if ticker_text else "")


def _threat_label(item: dict[str, Any]) -> str:
    level = str(item.get("level") or "").strip().casefold()
    label = _LEVEL_LABELS.get(level, level)
    score = item.get("score")
    if label and isinstance(score, int | float):
        return f"{label}（评分 {score}）"
    if label:
        return label
    if isinstance(score, int | float):
        return f"评分 {score}"
    return ""


def _source_label(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "").strip().casefold()
    source_label = _SOURCE_LABELS.get(source, source)
    source_instance = str(item.get("source_instance") or "").strip()
    if source_instance and source_instance.casefold() != source:
        return f"{source_label} · {source_instance}" if source_label else source_instance
    return source_label


def _format_duration(start: str, end: str) -> str:
    started_at = _parse_datetime(start)
    ended_at = _parse_datetime(end)
    if started_at is None or ended_at is None or ended_at < started_at:
        return ""
    seconds = int((ended_at - started_at).total_seconds())
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒" if seconds else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _markdown_to_plain_text(value: str) -> str:
    lines = []
    for raw_line in value.splitlines():
        line = raw_line
        if line.startswith("### "):
            line = line[4:]
        lines.append(line.replace("**", ""))
    return "\n".join(lines)


def _format_alert_time(value: str) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return value or "未知"
    china_time = parsed.astimezone(timezone(timedelta(hours=8)))
    return china_time.strftime("%Y-%m-%d %H:%M:%S")


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value or "")


def _delivered_key(alert_id: str, group_openid: str) -> str:
    group_hash = hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:16]
    return f"{ALERT_DELIVERED_PREFIX}:{alert_id}:{group_hash}"
