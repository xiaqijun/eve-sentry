from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Literal
from weakref import WeakValueDictionary

import httpx
from redis.asyncio import Redis

from eve_risk.clients.base import request_with_retries
from eve_risk.domain import (
    FleetCompositionItem,
    Killmail,
    LatestEngagement,
    Participant,
    RelatedBattleRef,
    RelatedBattleSide,
    RelatedBattleSummary,
    ShipRole,
    ZKillStats,
)

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
        self._inflight_guard = asyncio.Lock()
        self._inflight: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def _lock_for_key(self, cache_key: str) -> asyncio.Lock:
        async with self._inflight_guard:
            lock = self._inflight.get(cache_key)
            if lock is None:
                lock = asyncio.Lock()
                self._inflight[cache_key] = lock
            return lock

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

        lock = await self._lock_for_key(cache_key)
        async with lock:
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

    async def fetch_character_stats(self, character_id: int) -> ZKillStats:
        cache_key = f"zkill:v1:stats:{character_id}"
        cached = await self.redis.get(cache_key)
        if cached:
            raw = cached if isinstance(cached, bytes) else str(cached).encode()
            return ZKillStats.model_validate_json(raw)

        lock = await self._lock_for_key(cache_key)
        async with lock:
            cached = await self.redis.get(cache_key)
            if cached:
                raw = cached if isinstance(cached, bytes) else str(cached).encode()
                return ZKillStats.model_validate_json(raw)
            await self._wait_for_rate_slot()
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/stats/characterID/{character_id}/",
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"},
                timeout=45.0,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("zKillboard returned invalid character stats")
            stats = ZKillStats(
                character_id=character_id,
                ships_destroyed=int(payload.get("shipsDestroyed") or 0),
                ships_lost=int(payload.get("shipsLost") or 0),
                points_destroyed=int(payload.get("pointsDestroyed") or 0),
                isk_destroyed=float(payload.get("iskDestroyed") or 0),
                isk_lost=float(payload.get("iskLost") or 0),
                solo_kills=int(payload.get("soloKills") or 0),
                danger_ratio=float(payload.get("dangerRatio") or 0),
                gang_ratio=float(payload.get("gangRatio") or 0),
            )
            await self.redis.set(cache_key, stats.model_dump_json(), ex=self.cache_ttl_seconds)
            return stats

    async def fetch_related_battle(
        self, ref: RelatedBattleRef
    ) -> RelatedBattleSummary:
        occurred_at = ref.occurred_at.astimezone(UTC)
        stamp = occurred_at.strftime("%Y%m%d%H00")
        cache_key = f"zkill:v1:related:{ref.system_id}:{stamp}"
        cached = await self.redis.get(cache_key)
        if cached:
            raw = cached if isinstance(cached, bytes) else str(cached).encode()
            return RelatedBattleSummary.model_validate_json(raw)

        lock = await self._lock_for_key(cache_key)
        async with lock:
            cached = await self.redis.get(cache_key)
            if cached:
                raw = cached if isinstance(cached, bytes) else str(cached).encode()
                return RelatedBattleSummary.model_validate_json(raw)
            await self._wait_for_rate_slot()
            response = await request_with_retries(
                self.http,
                "GET",
                f"{self.base_url}/related/{ref.system_id}/{stamp}/",
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"},
                timeout=45.0,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("zKillboard returned invalid related battle data")
            summary = payload.get("summary") or {}
            result = RelatedBattleSummary(
                system_id=ref.system_id,
                occurred_at=occurred_at,
                team_a=_parse_related_side(summary.get("teamA") or {}),
                team_b=_parse_related_side(summary.get("teamB") or {}),
            )
            await self.redis.set(cache_key, result.model_dump_json(), ex=self.cache_ttl_seconds)
            return result

    async def enrich_related_battles(
        self,
        engagements: list[LatestEngagement],
        input_ids: set[int],
        *,
        limit: int = 5,
    ) -> list[LatestEngagement]:
        enriched: list[LatestEngagement] = []
        for index, engagement in enumerate(engagements):
            if index >= limit or not engagement.related_battle_refs:
                enriched.append(engagement)
                continue
            results = await asyncio.gather(
                *(self.fetch_related_battle(ref) for ref in engagement.related_battle_refs),
                return_exceptions=True,
            )
            summaries = [
                item for item in results if isinstance(item, RelatedBattleSummary)
            ]
            enriched.append(
                merge_related_battle_summaries(engagement, summaries, input_ids)
            )
        return enriched

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


def aggregate_character_stats(values: list[ZKillStats]) -> ZKillStats | None:
    if not values:
        return None
    activity_weights = [max(1, item.ships_destroyed + item.ships_lost) for item in values]
    kill_weights = [max(1, item.ships_destroyed) for item in values]
    activity_total = sum(activity_weights)
    kill_total = sum(kill_weights)
    return ZKillStats(
        ships_destroyed=sum(item.ships_destroyed for item in values),
        ships_lost=sum(item.ships_lost for item in values),
        points_destroyed=sum(item.points_destroyed for item in values),
        isk_destroyed=sum(item.isk_destroyed for item in values),
        isk_lost=sum(item.isk_lost for item in values),
        solo_kills=sum(item.solo_kills for item in values),
        danger_ratio=sum(
            item.danger_ratio * weight
            for item, weight in zip(values, activity_weights, strict=True)
        )
        / activity_total,
        gang_ratio=sum(
            item.gang_ratio * weight
            for item, weight in zip(values, kill_weights, strict=True)
        )
        / kill_total,
    )


def _parse_related_side(payload: dict[str, object]) -> RelatedBattleSide:
    entries = payload.get("list") or []
    character_ids: set[int] = set()
    ships: Counter[int] = Counter()
    ship_names: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        character_id = _optional_int(entry.get("characterID"))
        if character_id is not None:
            character_ids.add(character_id)
        if not entry.get("isVictim"):
            continue
        type_id = _optional_int(entry.get("shipTypeID"))
        if type_id is None:
            continue
        ships[type_id] += 1
        ship_names[type_id] = str(entry.get("shipName") or f"舰船 {type_id}")
    totals = payload.get("totals") or {}
    return RelatedBattleSide(
        character_ids=character_ids,
        loss_value=float(totals.get("total_price") or 0),
        ships_lost=int(totals.get("totalShips") or sum(ships.values())),
        pilot_count=int(totals.get("pilotCount") or len(character_ids)),
        lost_ships=[
            FleetCompositionItem(
                id=type_id,
                name=ship_names[type_id],
                role=ShipRole.OTHER.value,
                count=count,
            )
            for type_id, count in ships.most_common(4)
        ],
    )


def merge_related_battle_summaries(
    engagement: LatestEngagement,
    summaries: list[RelatedBattleSummary],
    input_ids: set[int],
) -> LatestEngagement:
    lost_value = 0.0
    destroyed_value = 0.0
    loss_count = 0
    destroyed_count = 0
    own_pilots: set[int] = set()
    lost_ships: Counter[int] = Counter()
    destroyed_ships: Counter[int] = Counter()
    ship_names: dict[int, str] = {}
    matched = False
    for summary in summaries:
        if input_ids.intersection(summary.team_a.character_ids):
            own, enemy = summary.team_a, summary.team_b
        elif input_ids.intersection(summary.team_b.character_ids):
            own, enemy = summary.team_b, summary.team_a
        else:
            continue
        matched = True
        lost_value += own.loss_value
        destroyed_value += enemy.loss_value
        loss_count += own.ships_lost
        destroyed_count += enemy.ships_lost
        own_pilots.update(own.character_ids)
        for item in own.lost_ships:
            if item.id is not None:
                lost_ships[item.id] += item.count
                ship_names[item.id] = item.name
        for item in enemy.lost_ships:
            if item.id is not None:
                destroyed_ships[item.id] += item.count
                ship_names[item.id] = item.name
    if not matched:
        return engagement

    def items(values: Counter[int]) -> list[FleetCompositionItem]:
        return [
            FleetCompositionItem(
                id=type_id,
                name=ship_names[type_id],
                role=ShipRole.OTHER.value,
                count=count,
            )
            for type_id, count in values.most_common(4)
        ]

    if lost_value > 0 and destroyed_value > 0:
        outcome = "舰队交战"
    elif destroyed_value > 0:
        outcome = "舰队获胜"
    else:
        outcome = "舰队损失"
    return engagement.model_copy(
        update={
            "outcome": outcome,
            "result_detail": "zKill related battle 舰队战损",
            "total_value": lost_value + destroyed_value,
            "destroyed_count": destroyed_count,
            "loss_count": loss_count,
            "destroyed_value": destroyed_value,
            "lost_value": lost_value,
            "fleet_size": len(own_pilots) or engagement.fleet_size,
            "destroyed_ships": items(destroyed_ships),
            "lost_ships": items(lost_ships),
        }
    )
