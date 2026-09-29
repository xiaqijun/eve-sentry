"""Client-side monitoring boundaries received from the authenticated server."""

from __future__ import annotations

from typing import Any


def allows_remote(scope: Any, name: Any, system_id: Any = None) -> bool:
    """Older servers remain compatible; unknown locations in an enabled scope stay local."""
    if not isinstance(scope, dict) or scope.get("enabled") is not True:
        return True
    key = str(name or "").strip().casefold()
    try:
        numeric_id = int(system_id) if system_id else None
    except (ValueError, TypeError):
        return False
    for item in scope.get("systems", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or "").strip().casefold()
        sid = item.get("system_id")
        if (key == label or (key in {"", "unknown"} and numeric_id is not None and numeric_id == sid)) and (numeric_id is None or sid == numeric_id):
            return True
    return False


def apply_window_scope(window, scope: Any) -> None:
    """Refresh only upload eligibility; never stop capture, OCR or local sounds."""
    if not isinstance(scope, dict) or type(scope.get("enabled")) is not bool:
        return
    previous = getattr(window, "_monitoring_scope", None)
    if previous == scope:
        return
    window._monitoring_scope = scope
    manager = getattr(window, "_upload_manager", None)
    apply = getattr(manager, "set_monitoring_scope", None)
    if callable(apply):
        apply(scope)
    controller = getattr(window, "_alert_controller", None)
    if controller is not None:
        controller._monitoring_scope = scope
    for key, context in getattr(window, "_worker_contexts", {}).items():
        local_only = not allows_remote(scope, context.get("system_name"), context.get("system_id"))
        window._log_message(f"{context.get('window_title') or 'EVE'}: " + ("区域外，仅本地预警" if local_only else "监控范围内，远端上报已启用"))
        if controller is not None:
            controller.update_local_hostile_count(context.get("system_name", "Unknown"), context.get("_hostile_icon_count", 0))
        worker = getattr(window, "_workers", {}).get(key)
        refresh = getattr(worker, "request_presence_refresh", None)
        if callable(refresh):
            refresh()
