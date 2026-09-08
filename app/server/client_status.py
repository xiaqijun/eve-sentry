"""Server-side helpers for interpreting client heartbeat snapshots."""

from __future__ import annotations

from typing import Any


def monitored_system_names(client_snapshot: Any) -> list[str]:
    """Return unique systems served by online monitoring detector nodes."""
    if not isinstance(client_snapshot, dict):
        return []
    heartbeats = client_snapshot.get("heartbeats")
    if not isinstance(heartbeats, list):
        return []

    systems: list[str] = []
    seen: set[str] = set()

    def add_system(value: Any) -> None:
        system = str(value or "").strip()
        key = system.casefold()
        if not system or key == "unknown" or key in seen:
            return
        seen.add(key)
        systems.append(system)

    for heartbeat in heartbeats:
        if not isinstance(heartbeat, dict):
            continue
        if str(heartbeat.get("client_type") or "") != "detector_client":
            continue
        if not bool(heartbeat.get("online")):
            continue
        details = heartbeat.get("details")
        if not isinstance(details, dict) or not bool(details.get("monitoring")):
            continue
        targets = details.get("targets")
        if isinstance(targets, list):
            active_target_seen = False
            target_system_seen = False
            for target in targets:
                if not isinstance(target, dict):
                    continue
                if not bool(target.get("monitoring", True)):
                    continue
                active_target_seen = True
                before = len(systems)
                add_system(target.get("system_name") or target.get("system"))
                target_system_seen = target_system_seen or len(systems) > before
            if active_target_seen and not target_system_seen:
                add_system(details.get("system_name") or details.get("system"))
            continue
        add_system(details.get("system_name") or details.get("system"))
    return systems
