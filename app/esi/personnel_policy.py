"""Deterministic refresh policy; no threads, requests, or runtime activation."""

from __future__ import annotations

import math
from dataclasses import dataclass


def classification_profile(profile: dict) -> dict:
    """Keep display data intact, but do not classify from untrusted old affiliation."""
    if profile.get("affiliation_trusted") is not False:
        return profile
    result = dict(profile)
    for key in ("corporation_id", "alliance_id", "faction_id"):
        result.pop(key, None)
    if result.get("standing_contact_type") != "character":
        for key in ("contact_standing", "standing"):
            result.pop(key, None)
    return result


DAY = 86400.0
NAME_REFRESH_SECONDS = 90 * DAY
# ESI POST /characters/affiliation: x-client-cache-ttl / x-server-cache-ttl.
# Activity controls queue priority, never extends or shortens data validity.
AFFILIATION_TTL = 3600.0
AFFILIATION_INTERVALS = dict.fromkeys(range(1, 6), AFFILIATION_TTL)


def timestamp(value: float) -> float:
    """Reject timestamps which cannot be ordered safely in the database."""
    result = float(value)
    if isinstance(value, bool) or not math.isfinite(result) or result < 0:
        raise ValueError("timestamp must be finite and non-negative")
    return result


def positive_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("entity ID must be a positive integer")
    return value


def name_key(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("confirmed name must not be empty")
    return name.strip().casefold()


def personnel_tier(*, now: float, last_seen_at: float, active: bool = False) -> int:
    age = max(0.0, timestamp(now) - timestamp(last_seen_at))
    if active:
        return 1
    if age <= DAY:
        return 2
    if age <= 7 * DAY:
        return 3
    if age <= 30 * DAY:
        return 4
    return 5


def refresh_due_at(
    *, character_id: int, fetched_at: float, tier: int,
    upstream_valid_until: float = 0.0, spare_capacity: bool = False,
) -> float:
    """All tiers expire together; spare capacity changes throughput, not TTL."""
    positive_id(character_id)
    if tier not in AFFILIATION_INTERVALS:
        raise ValueError("affiliation tier must be 1-5")
    # Keep keyword compatibility; neither a local legacy one-day deadline nor
    # extra workers can override the official one-hour affiliation cache policy.
    timestamp(upstream_valid_until)
    return timestamp(fetched_at) + AFFILIATION_TTL


def retry_delay(failures: int, *, jitter: float = 0.5, retry_after: float = 0.0) -> float:
    if failures < 1 or not 0 <= jitter <= 1:
        raise ValueError("invalid retry parameters")
    base = min(300.0, 5.0 * 2 ** min(6, failures - 1))
    return max(base * (0.8 + 0.2 * jitter), timestamp(retry_after))


@dataclass(frozen=True)
class RefreshLoad:
    """Inputs supplied by the future runtime scheduler, not sampled here."""

    realtime_pending: int = 0
    cpu_fraction: float = 0.0
    database_wait_ms: float = 0.0
    upstream_latency_ms: float = 0.0
    upstream_errors: bool = False
    throttled: bool = False


def background_slots(current: int, load: RefreshLoad, maximum: int = 4) -> int:
    """Grow gradually during idle periods; shed only background capacity."""
    if maximum < 1 or current < 0 or load.realtime_pending < 0:
        raise ValueError("invalid scheduler capacity")
    measurements = (load.cpu_fraction, load.database_wait_ms, load.upstream_latency_ms)
    if any(not math.isfinite(value) or value < 0 for value in measurements):
        return 0
    if load.realtime_pending or load.throttled or load.upstream_errors:
        return 0
    if load.cpu_fraction >= 0.8 or load.database_wait_ms >= 50 or load.upstream_latency_ms >= 2000:
        return max(0, min(current, maximum) - 1)
    return min(maximum, current + 1)
