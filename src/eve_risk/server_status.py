from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis

from eve_risk.alerts import ALERT_DEDUPE_SECONDS, ALERT_GROUPS_KEY
from eve_risk.clients.qq import QQOpenAPIClient

logger = logging.getLogger(__name__)

SERVER_STATUS_STATE_KEY = "qq:eve-server:status-state"
SERVER_STATUS_FAILURES_KEY = "qq:eve-server:status-failures"
SERVER_STARTUP_DELIVERED_PREFIX = "qq:eve-server:startup-delivered"


@dataclass(frozen=True, slots=True)
class EveServerStatus:
    players: int
    server_version: str
    start_time: datetime
    vip: bool

    @property
    def online(self) -> bool:
        return not self.vip and self.start_time.year > 1970


class EveServerStartupMonitor:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        qq: QQOpenAPIClient,
        status_url: str,
        *,
        poll_interval_seconds: float = 5.0,
        offline_threshold: int = 6,
    ) -> None:
        self.http = http
        self.redis = redis
        self.qq = qq
        self.status_url = status_url.strip()
        self.poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self.offline_threshold = max(1, int(offline_threshold))

    @property
    def enabled(self) -> bool:
        return bool(self.status_url)

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EVE server status check failed")
            await asyncio.sleep(self.poll_interval_seconds)

    async def check_once(self) -> bool:
        try:
            status = await self._fetch_status()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            await self._record_unavailable()
            return False

        if not status.online:
            await self._record_unavailable()
            return False

        await self.redis.delete(SERVER_STATUS_FAILURES_KEY)
        previous = await self._load_state()
        start_time = status.start_time.astimezone(UTC).isoformat()
        should_announce = bool(previous) and (
            previous.get("status") == "offline"
            or str(previous.get("start_time") or "") != start_time
        )
        await self._save_state({"status": "online", "start_time": start_time})
        if should_announce:
            await self._deliver_startup(status)
        return True

    async def _fetch_status(self) -> EveServerStatus:
        response = await self.http.get(self.status_url, timeout=3.0)
        response.raise_for_status()
        payload = response.json()
        start_time = datetime.fromisoformat(str(payload["start_time"]).replace("Z", "+00:00"))
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UTC)
        return EveServerStatus(
            players=max(0, int(payload.get("players") or 0)),
            server_version=str(payload.get("server_version") or "未知").strip() or "未知",
            start_time=start_time,
            vip=bool(payload.get("vip", False)),
        )

    async def _record_unavailable(self) -> None:
        failures = int(await self.redis.incr(SERVER_STATUS_FAILURES_KEY))
        await self.redis.expire(
            SERVER_STATUS_FAILURES_KEY,
            max(60, int(self.poll_interval_seconds * self.offline_threshold * 4)),
        )
        if failures < self.offline_threshold:
            return
        previous = await self._load_state()
        await self._save_state(
            {
                "status": "offline",
                "start_time": str(previous.get("start_time") or ""),
            }
        )
        if failures == self.offline_threshold:
            logger.info("EVE server downtime confirmed after %d checks", failures)

    async def _deliver_startup(self, status: EveServerStatus) -> None:
        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        started_at = status.start_time.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        message = (
            "🟢 EVE 服务器已开服\n"
            f"在线人数｜{status.players}\n"
            f"服务器版本｜{status.server_version}\n"
            f"启动时间｜{started_at}"
        )
        event_id = status.start_time.astimezone(UTC).isoformat()
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                logger.exception("QQ EVE server startup delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
        logger.info("EVE server startup processed groups=%d", len(groups))

    async def _load_state(self) -> dict[str, Any]:
        raw = await self.redis.get(SERVER_STATUS_STATE_KEY)
        if not raw:
            return {}
        try:
            payload = json.loads(_decode(raw))
        except (json.JSONDecodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _save_state(self, state: dict[str, str]) -> None:
        await self.redis.set(
            SERVER_STATUS_STATE_KEY,
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        )


def _delivered_key(event_id: str, group_openid: str) -> str:
    group_hash = hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:16]
    event_hash = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16]
    return f"{SERVER_STARTUP_DELIVERED_PREFIX}:{event_hash}:{group_hash}"


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore").strip()
    return str(value or "").strip()
