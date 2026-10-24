"""Resolve explicitly selected alert centers without guessing billing scope."""

from collections.abc import Mapping
from typing import Any


def resolve_alert_scope(scope: Any, systems: Mapping[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    """Resolve one versioned selection against the configured server map."""
    if not isinstance(scope, dict) or type(scope.get("version")) is not int or scope["version"] != 1:
        return None
    revision = scope.get("revision")
    selected = scope.get("systems")
    if not isinstance(revision, str) or not revision.strip() or len(revision) > 128:
        return None
    if not isinstance(selected, list) or len(selected) > 128:
        return None
    by_name = {}
    known_ids = set()
    for system in systems.values():
        system_id = getattr(system, "system_id", None)
        name = str(getattr(system, "name", "")).strip().casefold()
        if type(system_id) is int and system_id > 0 and name:
            by_name[name] = str(system_id)
            known_ids.add(str(system_id))
    resolved = set()
    for item in selected:
        if not isinstance(item, dict):
            return None
        name = item.get("name", "")
        if not isinstance(name, str):
            return None
        name = name.strip().casefold()
        supplied_id = item.get("id")
        system_id = None
        if supplied_id is not None:
            if type(supplied_id) not in (str, int):
                return None
            text_id = str(supplied_id)
            if len(text_id) > 10 or not text_id.isascii() or not text_id.isdecimal() or int(text_id) <= 0:
                return None
            system_id = str(int(text_id))
            if system_id not in known_ids:
                return None
        if name:
            named_id = by_name.get(name)
            if named_id is None or (system_id is not None and named_id != system_id):
                return None
            system_id = named_id
        if system_id is None:
            return None
        resolved.add(system_id)
    return revision, tuple(sorted(resolved))


def stable_alert_system_ids(
    previous: dict[str, Any], current: dict[str, Any], systems: Mapping[str, Any],
) -> tuple[str, ...]:
    """Skip intervals crossing reconnects, unresolved scopes or selection changes."""
    connection_id = current.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id.strip():
        return ()
    if previous.get("connection_id") != connection_id:
        return ()
    prior = resolve_alert_scope(previous.get("alert_scope"), systems)
    active = resolve_alert_scope(current.get("alert_scope"), systems)
    return active[1] if active is not None and prior == active else ()
