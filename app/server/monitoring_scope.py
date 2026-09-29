"""Server-owned monitoring boundaries; local capture is never disabled."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MonitoringScope:
    enabled: bool = False
    version: str = ""
    systems: tuple[tuple[str, int | None], ...] = ()
    _names: dict[str, int | None] = field(init=False, repr=False, compare=False)
    _ids: frozenset[int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_names", {name.casefold(): sid for name, sid in self.systems})
        object.__setattr__(self, "_ids", frozenset(sid for _, sid in self.systems if sid is not None))

    def allows(self, name: Any, system_id: Any = None) -> bool:
        if not self.enabled:
            return True
        key = str(name or "").strip().casefold()
        try:
            numeric_id = int(system_id) if system_id else None
        except (ValueError, TypeError):
            return False
        # Both fields, when supplied, must describe the same allowed system.
        if key in {"", "unknown"}:
            return numeric_id is not None and numeric_id in self._ids
        return key in self._names and (numeric_id is None or self._names[key] == numeric_id)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "version": self.version,
                "systems": [{"name": name, "system_id": sid} for name, sid in self.systems]}


def current_scope(store: Any) -> MonitoringScope:
    return getattr(store, "_monitoring_scope", MonitoringScope())


def in_scope(store: Any, payload: dict[str, Any]) -> bool:
    return current_scope(store).allows(
        payload.get("system_name") or payload.get("system"), payload.get("system_id"),
    )


def ignored_upload(store: Any) -> dict[str, Any]:
    return {"ok": True, "accepted": False, "ignored": True, "created": 0,
            "reason": "outside_monitoring_scope", "local_only": True,
            "monitoring_scope": current_scope(store).to_dict()}


def scoped_heartbeat(store: Any, heartbeat: dict[str, Any]) -> dict[str, Any]:
    """Keep diagnostic liveness, but exclude local-only windows from monitoring."""
    scope = current_scope(store)
    if not scope.enabled or heartbeat.get("client_type") != "detector_client":
        return heartbeat
    result = dict(heartbeat)
    details = dict(result.get("details") or {})
    targets = details.get("targets")
    if isinstance(targets, list) and targets:
        details["targets"] = [dict(target) for target in targets if isinstance(target, dict)]
        for target in details["targets"]:
            if not in_scope(store, target):
                target.update(monitoring=False, local_only=True, hostile_icon_count=0)
        details["monitoring"] = bool(details.get("monitoring")) and any(
            target.get("monitoring") for target in details["targets"]
        )
    elif not in_scope(store, details):
        details.update(monitoring=False, local_only=True, hostile_icon_count=0)
    result["details"] = details
    return result


def expire_outside_scope(store: Any, left_at: str) -> int:
    """Retire excluded current observations through the normal persistence path."""
    scope = current_scope(store)
    if not scope.enabled:
        return 0
    count = 0
    for item in store._active_intel.values():
        if not item.active and not (item.metadata.get("presence_only") and not item.metadata.get("left_reason")):
            continue
        if scope.allows(item.system_name, item.system_id):
            continue
        item.active = False
        item.left_at = left_at
        item.metadata["left_reason"] = "outside_monitoring_scope"
        store._ocr_missing_counts.pop(item.active_id, None)
        store._reset_ocr_alert_cooldown(item)
        count += 1
    return count
