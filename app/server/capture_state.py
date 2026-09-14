"""Validate and fence capture sessions; never trust upload retries as frames."""

import logging
import time
from typing import Any

from app.server.source_authority import (
    is_resident_presence,
    next_join_order,
    primary_sources,
)

CAPTURE_LEASE_SECONDS = 45.0
ROSTER_MISSING_GRACE_SECONDS = 15.0
logger = logging.getLogger(__name__)


def update_roster_wait(presence: Any, now: float) -> None:
    """Count accepted new frames without a matching nonempty OCR result."""
    metadata = presence.metadata
    capture = metadata.get("capture") or {}
    usable = (
        metadata.get("roster_has_names") is True
        and metadata.get("roster_quality_fingerprint") == capture.get("fingerprint")
    )
    if not capture or metadata.get("hostile_icon_count", 0) <= 0 or usable:
        for key in ("roster_missing_since", "roster_missing_frames", "roster_wait_sequence"):
            metadata.pop(key, None)
        if metadata.get("hostile_icon_count", 0) <= 0:
            for key in ("roster_has_names", "roster_quality_samples", "roster_quality_fingerprint",
                        "roster_observed_at"):
                metadata.pop(key, None)
        return
    metadata.setdefault("roster_missing_since", now)
    sequence = capture["sequence"]
    if sequence > metadata.get("roster_wait_sequence", 0):
        metadata["roster_missing_frames"] = metadata.get("roster_missing_frames", 0) + 1
        metadata["roster_wait_sequence"] = sequence


def reconcile_missing_roster(items: Any, system: str, now: float) -> list[Any]:
    """Promote a proven standby, never infer safety from missing OCR."""
    rows = list(items)
    primary = primary_sources(rows).get(system.casefold())
    if primary is None:
        return []
    metadata = primary.metadata
    since = metadata.get("roster_missing_since")
    if (metadata.get("hostile_icon_count", 0) <= 0 or since is None
            or now - since < ROSTER_MISSING_GRACE_SECONDS
            or metadata.get("roster_missing_frames", 0) < 3):
        return []
    residents = [item for item in rows if is_resident_presence(item)
                 and item.system_name.casefold() == system.casefold()]
    qualified = []
    for item in residents:
        meta = item.metadata
        capture = meta.get("capture") or {}
        if (item is primary or meta.get("hostile_icon_count", 0) <= 0
                or meta.get("roster_has_names") is not True
                or not meta.get("roster_quality_samples")
                or meta["roster_quality_samples"][-1][1] != "complete"
                or meta.get("roster_quality_fingerprint") != capture.get("fingerprint")
                or not 0 <= now - float(meta.get("capture_received_at") or 0) <= CAPTURE_LEASE_SECONDS
                or not 0 <= now - float(meta.get("roster_observed_at") or 0) <= CAPTURE_LEASE_SECONDS):
            continue
        qualified.append(item)
    if not qualified:
        return []
    chosen = primary_sources(qualified)[system.casefold()]
    changed = []
    remaining = list(residents)
    while primary_sources(remaining)[system.casefold()] is not chosen:
        displaced = primary_sources(remaining)[system.casefold()]
        remaining.remove(displaced)
        displaced.metadata["source_join_order"] = next_join_order(rows)
        displaced.metadata["authority_change_reason"] = "roster_missing"
        changed.append(displaced)
    logger.info("Monitoring authority changed: system=%s reason=roster_missing", system)
    return changed


def capture_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("capture")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("capture must be an object")  # noqa: TRY004 -- HTTP validation maps ValueError to 400.
    result = {}
    for key in ("session_epoch", "sequence"):
        value = raw.get(key)
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError(f"capture.{key} must be a positive int64")
        result[key] = value
    for key in ("session_id", "fingerprint"):
        value = raw.get(key)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"capture.{key} is invalid")
        result[key] = value
    quality = raw.get("roster_quality", "unknown")
    if quality not in {"complete", "truncated", "unknown"}:
        raise ValueError("capture.roster_quality is invalid")
    result["roster_quality"] = quality
    return result


def capture_is_current(
    items: Any, client_id: str, capture: dict, *, ocr: bool = False
) -> bool:
    """Fence epochs across systems; OCR may lag an unchanged captured image."""
    previous = [
        item.metadata.get("capture")
        for item in items
        if item.metadata.get("presence_only")
        and item.metadata.get("client_id") == client_id
        and item.metadata.get("capture")
    ]
    if not previous:
        return not ocr or not capture  # A modern OCR upload needs its Presence first.
    if not capture:
        return False  # Do not allow an older binary to bypass an established session fence.
    latest = max(
        previous, key=lambda value: (value["session_epoch"], value["sequence"])
    )
    if capture["session_epoch"] != latest["session_epoch"]:
        return not ocr and capture["session_epoch"] > latest["session_epoch"]
    if capture["session_id"] != latest["session_id"]:
        return False
    if ocr:
        return (
            capture["sequence"] <= latest["sequence"]
            and capture["fingerprint"] == latest["fingerprint"]
        )
    return capture["sequence"] >= latest["sequence"]


def capture_guard(items: Any, client_id: str) -> tuple:
    captures = [
        item.metadata.get("capture")
        for item in items
        if item.metadata.get("presence_only")
        and item.metadata.get("client_id") == client_id
        and item.metadata.get("capture")
    ]
    if not captures:
        return ()
    latest = max(
        captures, key=lambda value: (value["session_epoch"], value["sequence"])
    )
    return latest["session_epoch"], latest["session_id"], latest["fingerprint"]


def record_roster_quality(
    items: Any, client_id: str, system: str, capture: dict, *, has_names: bool | None = None
) -> list[Any]:
    """Record OCR outcomes and demote failing primaries only with a good standby."""
    if not capture:
        return []
    rows = list(items)
    presence = next(
        (
            item
            for item in rows
            if is_resident_presence(item)
            and item.metadata.get("client_id") == client_id
            and item.system_name.casefold() == system.casefold()
        ),
        None,
    )
    if presence is None:
        return []
    samples = list(presence.metadata.get("roster_quality_samples") or [])
    seq = capture["sequence"]
    if samples and seq <= samples[-1][0]:
        return []
    quality = "unknown" if has_names is False else capture["roster_quality"]
    samples = (samples + [[seq, quality]])[-5:]
    presence.metadata["roster_quality_samples"] = samples
    presence.metadata["roster_quality_fingerprint"] = capture["fingerprint"]
    presence.metadata["roster_has_names"] = (
        capture["roster_quality"] == "complete" if has_names is None else has_names
    )
    now = time.time()
    presence.metadata["roster_observed_at"] = now
    update_roster_wait(presence, now)
    missing_changes = reconcile_missing_roster(rows, system, now)
    if missing_changes:
        return [presence] + [item for item in missing_changes if item is not presence]
    primary = primary_sources(rows).get(system.casefold())
    if primary is not None:
        bad = primary.metadata.get("roster_quality_samples") or []
        repeated = sum(sample[1] == "truncated" for sample in bad) >= 3
        residents = [
            item
            for item in rows
            if is_resident_presence(item)
            and item.system_name.casefold() == system.casefold()
        ]
        qualified = [
            item
            for item in residents
            if item is not primary
            and (
                item.metadata.get("hostile_icon_count") == 0
                or (
                    (item.metadata.get("roster_quality_samples") or [[0, "unknown"]])[
                        -1
                    ][1]
                    == "complete"
                    and item.metadata.get("roster_quality_fingerprint")
                    == (item.metadata.get("capture") or {}).get("fingerprint")
                )
            )
            and time.time() - float(item.metadata.get("capture_received_at") or 0)
            <= CAPTURE_LEASE_SECONDS
        ]
        if repeated and qualified:
            chosen = primary_sources(qualified)[system.casefold()]
            changed = [presence]
            # Unqualified older standby rows must not get selected ahead of
            # the known-good takeover candidate. Requeue them in old order.
            remaining = list(residents)
            while primary_sources(remaining)[system.casefold()] is not chosen:
                displaced = primary_sources(remaining)[system.casefold()]
                remaining.remove(displaced)
                displaced.metadata["source_join_order"] = next_join_order(rows)
                if displaced not in changed:
                    changed.append(displaced)
            return changed
    return [presence]


def expire_captures(items: Any, now: float) -> int:
    """Heartbeat liveness alone cannot extend a stopped capture loop's lease."""
    rows = list(items)
    stale = {
        item.metadata.get("client_id")
        for item in rows
        if is_resident_presence(item)
        and item.metadata.get("capture_received_at")
        and now - float(item.metadata["capture_received_at"]) > CAPTURE_LEASE_SECONDS
    }
    changed = 0
    for item in rows:
        if (
            item.source == "eve-sentry-detector"
            and item.metadata.get("client_id") in stale
            and (item.active or is_resident_presence(item))
        ):
            item.active = False
            item.metadata["left_reason"] = "capture_stale"
            changed += 1
    return changed


def primary_clear_event(
    items: Any, client_id: str, system: str, occurred_at: str
) -> dict | None:
    """A valid primary zero also reconciles a retained unknown projection."""
    primary = primary_sources(items).get(system.casefold())
    if (
        primary is None
        or primary.metadata.get("client_id") != client_id
        or primary.metadata.get("hostile_icon_count") != 0
    ):
        return None
    generation = int(primary.metadata.get("source_join_order") or 0)
    return {
        "event_key": f"primary.clear:{system.casefold()}:{client_id}:{generation}:{occurred_at}",
        "event_type": "alert.cleared",
        "entity_key": system.casefold(),
        "occurred_at": occurred_at,
        "payload": {
            "system_name": primary.system_name,
            "system_id": primary.system_id,
            "hostile_count": 0,
            "active": False,
            "hostile_personnel": [],
            "primary_client_id": client_id,
            "primary_generation": generation,
            "freshness": "fresh",
        },
    }


def reconcile_zero_events(
    events: list[dict],
    items: Any,
    occurred_at: str,
    *,
    previous_zeros=(),
    positive_systems=(),
) -> list[dict]:
    """Project valid zero takeovers and zero-source loss without fake safety."""
    rows = list(items)
    primaries = primary_sources(rows)
    by_system = {event["entity_key"]: event for event in events}
    for key, primary in primaries.items():
        if key in positive_systems:
            continue  # An independent manual/channel observation remains active.
        clear = primary_clear_event(
            rows,
            primary.metadata.get("client_id", ""),
            primary.system_name,
            occurred_at,
        )
        if clear is not None:
            by_system[key] = clear
    for primary in previous_zeros:
        key = primary.system_name.casefold()
        if key in primaries or key in by_system:
            continue
        if primary.metadata.get("left_reason") not in {
            "heartbeat_stale",
            "capture_stale",
            "target_removed",
            "monitor_stopped",
            "system_changed",
        }:
            continue
        generation = int(primary.metadata.get("source_join_order") or 0)
        by_system[key] = {
            "event_key": f"primary.unknown:{key}:{generation}:{occurred_at}",
            "event_type": "alert.updated",
            "entity_key": key,
            "occurred_at": occurred_at,
            "payload": {
                "system_name": primary.system_name,
                "system_id": primary.system_id,
                "hostile_count": 0,
                "active": True,
                "hostile_personnel": [],
                "primary_client_id": primary.metadata.get("client_id", ""),
                "primary_generation": generation,
                "freshness": "unknown",
            },
        }
    return list(by_system.values())
