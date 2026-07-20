from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
from redis.asyncio import Redis

from eve_risk.clients.qq import QQOpenAPIClient

logger = logging.getLogger(__name__)

ALERT_GROUPS_KEY = "qq:eve-sentry:alert-groups"
ALERT_CURSOR_KEY = "qq:eve-sentry:alert-cursor"
ALERT_DELIVERED_PREFIX = "qq:eve-sentry:alert-delivered"
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


def format_alert_message(alert: dict[str, Any], public_url: str = "") -> str:
    raw_names = alert.get("names")
    names = [str(name).strip() for name in raw_names or [] if str(name).strip()]
    if len(names) > 5:
        targets = "、".join(names[:5]) + f" 等 {len(names)} 人"
    else:
        targets = "、".join(names) or str(alert.get("target") or "未知目标")

    level = str(alert.get("level") or "").strip().casefold()
    level_label = _LEVEL_LABELS.get(level, level or "未知")
    score = alert.get("score")
    score_text = f"（评分 {score}）" if isinstance(score, int | float) else ""
    system_name = str(alert.get("system_name") or "未知星系").strip() or "未知星系"
    created_at = _format_alert_time(str(alert.get("created_at") or ""))

    lines = [
        "【EVE Sentry 敌对预警】",
        f"星系：{system_name}",
        f"目标：{targets}",
        f"等级：{level_label}{score_text}",
        f"时间：{created_at}",
    ]
    normalized_url = public_url.strip().rstrip("/")
    if normalized_url:
        lines.append(f"态势图：{normalized_url}")
    return "\n".join(lines)


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
        min_level: str = "",
        public_url: str = "",
        reconnect_delay_seconds: float = 3.0,
    ) -> None:
        self.http = http
        self.redis = redis
        self.qq = qq
        self.events_url = events_url.strip()
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

    async def deliver(self, alert: dict[str, Any]) -> None:
        alert_id = str(alert.get("id") or "").strip()
        created_at = str(alert.get("created_at") or "").strip()
        if not alert_id or not created_at:
            logger.warning("Ignored malformed EVE Sentry alert")
            return

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_alert_message(alert, self.public_url)
        delivered = 0
        for group_openid in groups:
            delivered_key = _delivered_key(alert_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                logger.exception("QQ proactive alert delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        await self.redis.set(ALERT_CURSOR_KEY, created_at)
        logger.info("EVE Sentry alert processed deliveries=%d", delivered)

    async def _stream_once(self) -> None:
        cursor = _decode(await self.redis.get(ALERT_CURSOR_KEY))
        params = {
            "limit": "50",
            "timeout": "30",
            "heartbeat": "15",
            "bootstrap": "0",
            "since": cursor or datetime.now(UTC).isoformat(),
        }
        if self.min_level:
            params["min_level"] = self.min_level
        timeout = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)
        async with self.http.stream(
            "GET",
            self.events_url,
            params=params,
            headers={"Accept": "text/event-stream"},
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for event_name, _event_id, data in iter_sse_events(
                response.aiter_lines()
            ):
                if event_name != "alert":
                    continue
                payload = json.loads(data)
                if isinstance(payload, dict):
                    await self.deliver(payload)


def _format_alert_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value or "未知"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    china_time = parsed.astimezone(timezone(timedelta(hours=8)))
    return china_time.strftime("%Y-%m-%d %H:%M:%S")


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value or "")


def _delivered_key(alert_id: str, group_openid: str) -> str:
    group_hash = hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:16]
    return f"{ALERT_DELIVERED_PREFIX}:{alert_id}:{group_hash}"
