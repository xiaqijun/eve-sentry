"""Helpers for building runtime heartbeat payload details."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from importlib import metadata
from typing import Any

from app.version import current_version


def heartbeat_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_runtime_identity() -> dict[str, str]:
    """Return stable runtime identity fields for heartbeat diagnostics."""
    version = str(os.environ.get("EVE_SENTRY_CLIENT_VERSION") or "").strip()
    if not version:
        try:
            version = metadata.version("eve-sentry")
        except metadata.PackageNotFoundError:
            version = current_version()
    host = (
        str(os.environ.get("EVE_SENTRY_CLIENT_HOST") or "").strip()
        or str(os.environ.get("COMPUTERNAME") or "").strip()
        or socket.gethostname().strip()
        or "unknown-host"
    )
    return {
        "client_version": version,
        "host": host,
    }


def summarize_heartbeat_error(message: str, max_length: int = 120) -> str:
    """Return a compact single-line error summary for heartbeat diagnostics."""
    text = " ".join(str(message or "").strip().split())
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return f"{text[: max_length - 3].rstrip()}..."


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
            # A target can be active before its local system is resolved. In
            # that case the heartbeat-level system is still a useful fallback.
            # When every target is explicitly stopped, however, the old
            # heartbeat-level location must not keep a node online.
            if active_target_seen and not target_system_seen:
                add_system(details.get("system_name") or details.get("system"))
            continue
        add_system(details.get("system_name") or details.get("system"))
    return systems


def _add_runtime_identity(
    details: dict[str, object],
    client_version: str,
    host: str,
    last_success_at: str,
) -> None:
    version = str(client_version or "").strip()
    if version:
        details["client_version"] = version
    hostname = str(host or "").strip()
    if hostname:
        details["host"] = hostname
    success_at = str(last_success_at or "").strip()
    if success_at:
        details["last_success_at"] = success_at


def build_detector_heartbeat_details(
    monitoring: bool,
    system_name: str,
    system_source: str,
    popup_alerts: bool,
    window_title: str,
    last_action: str = "",
    last_error: str = "",
    client_version: str = "",
    host: str = "",
    last_success_at: str = "",
) -> dict[str, object]:
    """Return compact detector-client heartbeat details for status views."""
    details: dict[str, object] = {
        "mode": "monitoring" if monitoring else "idle",
    }
    action = str(last_action or "").strip()
    if action:
        details["last_action"] = action
    error = summarize_heartbeat_error(last_error)
    if error:
        details["last_error"] = error
    _add_runtime_identity(details, client_version, host, last_success_at)
    details.update(
        {
            "monitoring": monitoring,
            "system": system_name or "Unknown",
            "system_source": system_source or "default",
            "popup": popup_alerts,
        }
    )
    title = str(window_title or "").strip()
    if title:
        details["window"] = title
    return details


def build_alert_heartbeat_details(
    transport: str,
    popup: bool,
    details_enabled: bool,
    last_action: str = "",
    last_error: str = "",
    client_version: str = "",
    host: str = "",
    last_success_at: str = "",
) -> dict[str, object]:
    """Return compact alert-client heartbeat details for status views."""
    details: dict[str, object] = {
        "mode": str(transport or "poll").strip() or "poll",
    }
    action = str(last_action or "").strip()
    if action:
        details["last_action"] = action
    error = summarize_heartbeat_error(last_error)
    if error:
        details["last_error"] = error
    _add_runtime_identity(details, client_version, host, last_success_at)
    details.update(
        {
            "transport": str(transport or "poll").strip() or "poll",
            "popup": popup,
            "details": details_enabled,
        }
    )
    return details


def build_channel_heartbeat_details(
    server_parse: bool,
    last_action: str = "",
    last_error: str = "",
    client_version: str = "",
    host: str = "",
    last_success_at: str = "",
) -> dict[str, object]:
    """Return compact channel-client heartbeat details for status views."""
    mode = "server_parse" if server_parse else "observation"
    details: dict[str, object] = {"mode": mode}
    action = str(last_action or "").strip()
    if action:
        details["last_action"] = action
    error = summarize_heartbeat_error(last_error)
    if error:
        details["last_error"] = error
    _add_runtime_identity(details, client_version, host, last_success_at)
    details["server_parse"] = server_parse
    return details
