"""Short-lived, member-scoped query conditions; never cache query results."""

from __future__ import annotations

import hashlib
import json
import logging

from redis.asyncio import Redis

logger = logging.getLogger(__name__)
QUERY_HISTORY_TTL_SECONDS = 600


def _history_key(group: str, member: str) -> str:
    scope = json.dumps([group, member], separators=(",", ":")).encode()
    return "qq:query:last:" + hashlib.sha256(scope).hexdigest()


def _validated_query(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    if mode in ("node_hostiles", "monitoring_nodes", "all_nodes"):
        return {"mode": mode} if set(value) == {"mode"} else None
    fields = ("system_name",) if mode == "system_roster" else (
        ("name", "corporation", "alliance") if mode == "filtered" else ()
    )
    for field in fields:
        target = value.get(field)
        if set(value) == {"mode", field} and isinstance(target, str) and target.strip():
            return {"mode": mode, field: target.strip()}
    return None


async def remember_query(redis: Redis, group: str, member: str, query: dict[str, str]) -> None:
    validated = _validated_query(query)
    if not group or not member or validated is None:
        return
    try:
        await redis.set(
            _history_key(group, member), json.dumps(validated, ensure_ascii=False),
            ex=QUERY_HISTORY_TTL_SECONDS,
        )
    except Exception:
        logger.warning("Could not save recent query conditions")


async def recall_query(redis: Redis, group: str, member: str) -> dict[str, str] | None:
    if not group or not member:
        return None
    try:
        raw = await redis.get(_history_key(group, member))
        return _validated_query(json.loads(raw)) if raw else None
    except Exception:
        logger.warning("Could not read recent query conditions")
        return None
