from __future__ import annotations

import asyncio
import gzip
import json
import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from redis.asyncio import Redis

from eve_risk.clients.base import request_with_retries
from eve_risk.domain import Killmail, Participant

RATE_SLOT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local last = tonumber(redis.call('GET', key) or '0')
local slot = now
if last + interval > slot then slot = last + interval end
redis.call('SET', key, slot, 'PX', math.ceil((slot - now) + interval * 4))
return slot - now
"""


class ZKillFetchResult:
    def __init__(
        self,
        character_id: int,
        direction: str,
        killmails: list[Killmail],
        *,
        truncated: bool = False,
        from_cache: bool = False,
    ) -> None:
        self.character_id = character_id
        self.direction = direction
        self.killmails = killmails
        self.truncated = truncated
        self.from_cache = from_cache


class ZKillClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        base_url: str,
        user_agent: str,
        request_interval_seconds: float = 1.2,
        cache_ttl_seconds: int = 1800,
    ) -> None:
        if not user_agent:
            raise RuntimeError("A real ZKILL_USER_AGENT is required")
        self.http = http
        self.redis = redis
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.interval_ms = int(request_interval_seconds * 1000)
        self.cache_ttl_seconds = cache_ttl_seconds
        self._fallback_lock = asyncio.Lock()
        self._fallback_next_slot = 0.0

    async def fetch_character(
        self, character_id: int, direction: Literal["kills", "losses"]
    ) -> ZKillFetchResult:
        cache_key = f"zkill:v1:{direction}:{character_id}"
        cached = await self.redis.get(cache_key)
        if cached:
            raw = cached if isinstance(cached, bytes) else str(cached).encode()
            try:
                raw = gzip.decompress(raw)
            except gzip.BadGzipFile:
                pass
            payload = json.loads(raw)
            return ZKillFetchResult(
                character_id,
                direction,
                [self._parse_killmail(item) for item in payload],
                truncated=len(payload) >= 1000,
                from_cache=True,
            )

        await self._wait_for_rate_slot()
        response = await request_with_retries(
            self.http,
            "GET",
            f"{self.base_url}/{direction}/characterID/{character_id}/",
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"},
            timeout=45.0,
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("zKillboard returned a non-list payload")
        encoded = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
        await self.redis.set(cache_key, encoded, ex=self.cache_ttl_seconds)
        return ZKillFetchResult(
            character_id,
            direction,
            [self._parse_killmail(item) for item in payload],
            truncated=len(payload) >= 1000,
        )

    async def _wait_for_rate_slot(self) -> None:
        now_ms = int(time.time() * 1000)
        try:
            wait_ms = int(
                await self.redis.eval(
                    RATE_SLOT_SCRIPT,
                    1,
                    "zkill:global-rate-slot",
                    now_ms,
                    self.interval_ms,
                )
            )
        except Exception:
            # Keep courtesy limiting in-process if Redis scripting is unavailable.
            async with self._fallback_lock:
                now = time.monotonic()
                slot = max(now, self._fallback_next_slot)
                self._fallback_next_slot = slot + self.interval_ms / 1000
                wait_ms = int(max(0.0, slot - now) * 1000)
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

    @staticmethod
    def _parse_killmail(payload: dict[str, object]) -> Killmail:
        victim = payload.get("victim") or {}
        participants = [
            Participant(
                character_id=_optional_int(victim.get("character_id")),
                corporation_id=_optional_int(victim.get("corporation_id")),
                alliance_id=_optional_int(victim.get("alliance_id")),
                ship_type_id=_optional_int(victim.get("ship_type_id")),
                is_victim=True,
            )
        ]
        for attacker in payload.get("attackers") or []:
            participants.append(
                Participant(
                    character_id=_optional_int(attacker.get("character_id")),
                    corporation_id=_optional_int(attacker.get("corporation_id")),
                    alliance_id=_optional_int(attacker.get("alliance_id")),
                    ship_type_id=_optional_int(attacker.get("ship_type_id")),
                    final_blow=bool(attacker.get("final_blow", False)),
                )
            )
        zkb = payload.get("zkb") or {}
        timestamp = datetime.fromisoformat(str(payload["killmail_time"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return Killmail(
            killmail_id=int(payload["killmail_id"]),
            killmail_time=timestamp,
            solar_system_id=int(payload["solar_system_id"]),
            participants=participants,
            solo=bool(zkb.get("solo", False)),
            total_value=float(zkb["totalValue"]) if zkb.get("totalValue") is not None else None,
        )


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None
