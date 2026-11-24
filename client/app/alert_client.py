"""Local tray alert client that consumes server alerts and local EVE windows."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import queue
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from app.monitoring_scope import allows_remote

from PyQt6.QtCore import QEvent, QPoint, QRect, QTimer, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QPainter, QPainterPath, QPen
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.core.heartbeat import (
    heartbeat_now_iso,
    monitored_system_names,
    resolve_runtime_identity,
    summarize_heartbeat_error,
)
from app.core.client_identity import persistent_client_id
from app.channels.local_system import find_latest_local_system
from app.channels.log_watcher import DEFAULT_CHATLOG_DIR
from app.engine.capturer import Capturer
from app.intel_client import MAX_MAP_NEIGHBORHOOD_HOPS, IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)

ALERT_CLIENT_TYPE = "alert_client"
ALERT_CLIENT_LABEL = "Alert Client"
DEFAULT_EVENT_TIMEOUT = 30.0
DEFAULT_HEARTBEAT_INTERVAL = 10.0
DEFAULT_RECONNECT_MAX_DELAY = 30.0
DEFAULT_ALERT_RANGE_HOPS = 5
ALERT_ACK_PROTOCOL = "alert-ack.v1"
OVERLAY_MIN_WIDTH = 320
OVERLAY_MIN_HEIGHT = 160
OVERLAY_RESIZE_MARGIN = 6
OVERLAY_HORIZONTAL_RESIZE_MARGIN = 12
RESIZE_LEFT = 1
RESIZE_RIGHT = 2
RESIZE_TOP = 4
RESIZE_BOTTOM = 8


def default_state_path() -> str:
    """Return the default per-user alert client state path."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return str(Path(local_app_data) / "EVE Sentry" / "alert_client_state.json")
    return str(Path.home() / ".eve-sentry" / "alert_client_state.json")


def _state_event_sequence(value: object) -> int | None:
    text = str(value or "").strip()
    if not text.startswith("state:"):
        return None
    try:
        return max(0, int(text.split(":", 1)[1]))
    except (TypeError, ValueError):
        return None


class AlertClientState:
    """Persist alert de-duplication and local-map selection preferences."""

    def __init__(
        self,
        path: str | Path = default_state_path(),
        max_seen_ids: int = 1000,
    ) -> None:
        self.path = Path(path)
        self.max_seen_ids = max(1, int(max_seen_ids))
        self.loaded = False
        self._seen_ids: list[str] = []
        self._seen_set: set[str] = set()
        self._last_event_id = ""
        self._map_selected_account_keys: list[str] | None = None
        self._overlay_geometry: dict[str, int] | None = None
        self._alert_range_hops = DEFAULT_ALERT_RANGE_HOPS

    def load_seen_ids(self) -> list[str]:
        """Load remembered alert ids from disk."""
        self._alert_range_hops = DEFAULT_ALERT_RANGE_HOPS
        self.loaded = self.path.exists()
        if not self.loaded:
            self._set_ids([])
            self._last_event_id = ""
            self._set_map_selected_account_keys(None)
            self._overlay_geometry = None
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read alert client state from %s", self.path)
            self._set_ids([])
            self._last_event_id = ""
            self._set_map_selected_account_keys(None)
            self._overlay_geometry = None
            return []
        if not isinstance(payload, dict):
            self._set_ids([])
            self._last_event_id = ""
            self._set_map_selected_account_keys(None)
            self._overlay_geometry = None
            return []
        self._set_ids(self._clean_ids(payload.get("seen_alert_ids")))
        self._last_event_id = str(payload.get("last_event_id") or "").strip()
        self._set_map_selected_account_keys(payload.get("map_selected_account_keys"))
        self._overlay_geometry = self._clean_overlay_geometry(
            payload.get("overlay_geometry")
        )
        hops = payload.get("alert_range_hops")
        if type(hops) is int and 0 <= hops <= MAX_MAP_NEIGHBORHOOD_HOPS:
            self._alert_range_hops = hops
        return list(self._seen_ids)

    def has_seen(self, alert_id: str) -> bool:
        """Return whether an alert id has already been delivered locally."""
        return str(alert_id or "").strip() in self._seen_set

    def record_alert(self, alert: dict[str, Any]) -> bool:
        """Remember one alert id, returning False when it was already known."""
        alert_id = str(alert.get("id") or "").strip()
        if not alert_id or alert_id in self._seen_set:
            return False
        ids = [item for item in self._seen_ids if item != alert_id]
        ids.append(alert_id)
        self.save_seen_ids(ids)
        return True

    def save_seen_ids(self, seen_ids: list[str]) -> None:
        """Persist normalized alert ids."""
        self._set_ids(self._clean_ids(seen_ids))
        self._write_state()

    def map_selected_account_keys(self) -> list[str] | None:
        """Return the saved local-map selection, or None before first selection."""
        if self._map_selected_account_keys is None:
            return None
        return list(self._map_selected_account_keys)

    def save_map_selected_account_keys(self, account_keys: list[str]) -> None:
        """Persist an explicit local-map account selection."""
        self._set_map_selected_account_keys(account_keys)
        self._write_state()

    def alert_range_hops(self) -> int:
        return self._alert_range_hops

    def save_alert_range_hops(self, hops: int) -> None:
        if self._alert_range_hops != hops:
            self._alert_range_hops = hops
            self._write_state()

    def last_event_id(self) -> str:
        """Return the last fully handled SSE event cursor."""
        return self._last_event_id

    def save_last_event_id(self, event_id: str) -> None:
        """Persist the last fully handled SSE event cursor."""
        cleaned = str(event_id or "").strip()
        if not cleaned or cleaned == self._last_event_id:
            return
        current_state_seq = _state_event_sequence(self._last_event_id)
        candidate_state_seq = _state_event_sequence(cleaned)
        if current_state_seq is not None and (
            candidate_state_seq is None or candidate_state_seq < current_state_seq
        ):
            # Presence/report/node IDs are useful for de-duplication but cannot
            # resume the ordered state log. Never replace a durable cursor with
            # a weaker ID, and never let an older sequence move it backwards.
            return
        self._last_event_id = cleaned
        self._write_state()

    def overlay_geometry(self) -> dict[str, int] | None:
        """Return the saved overlay rectangle."""
        return dict(self._overlay_geometry) if self._overlay_geometry else None

    def save_overlay_geometry(self, geometry: dict[str, Any]) -> None:
        """Persist the latest user-positioned overlay rectangle."""
        cleaned = self._clean_overlay_geometry(geometry)
        if cleaned is None or cleaned == self._overlay_geometry:
            return
        self._overlay_geometry = cleaned
        self._write_state()

    def _write_state(self) -> None:
        payload: dict[str, Any] = {"version": 4, "seen_alert_ids": self._seen_ids}
        if self._last_event_id:
            payload["last_event_id"] = self._last_event_id
        if self._map_selected_account_keys is not None:
            payload["map_selected_account_keys"] = self._map_selected_account_keys
        if self._overlay_geometry is not None:
            payload["overlay_geometry"] = self._overlay_geometry
        payload["alert_range_hops"] = self._alert_range_hops
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.loaded = True
        except OSError:
            logger.warning("Failed to write alert client state to %s", self.path)

    def _set_ids(self, seen_ids: list[str]) -> None:
        self._seen_ids = list(seen_ids)
        self._seen_set = set(seen_ids)

    def _set_map_selected_account_keys(self, account_keys: Any) -> None:
        if not isinstance(account_keys, list):
            self._map_selected_account_keys = None
            return
        self._map_selected_account_keys = self._clean_map_account_keys(account_keys)

    def _clean_ids(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value[-self.max_seen_ids:]:
            alert_id = str(item or "").strip()
            if not alert_id or alert_id in seen:
                continue
            seen.add(alert_id)
            cleaned.append(alert_id)
        return cleaned

    @staticmethod
    def _clean_map_account_keys(value: list[Any]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            account_key = str(item or "").strip()
            if not account_key or account_key in seen:
                continue
            seen.add(account_key)
            cleaned.append(account_key)
        return cleaned

    @staticmethod
    def _clean_overlay_geometry(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            geometry = {
                key: int(value[key]) for key in ("x", "y", "width", "height")
            }
        except (KeyError, TypeError, ValueError):
            return None
        if geometry["width"] <= 0 or geometry["height"] <= 0:
            return None
        return geometry


class AlertEventConsumer:
    """Apply local de-duplication before an alert reaches the UI."""

    def __init__(self, state: AlertClientState) -> None:
        self.state = state

    def accept(self, alert: dict[str, Any]) -> bool:
        """Return True only for locally new server-side alerts."""
        return self.state.record_alert(alert)


def alert_system_name(alert: dict[str, Any]) -> str:
    """Extract the display system name from a server alert."""
    return str(
        alert.get("system_name")
        or alert.get("system")
        or alert.get("solar_system_name")
        or "Unknown"
    ).strip() or "Unknown"


def alert_hostile_count(alert: dict[str, Any]) -> int:
    """Return the number shown as 敌:x for an alert."""
    for key in ("hostile_count", "target_count", "count"):
        value = alert.get(key)
        if value not in {None, ""}:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            return max(1, number)

    metadata = alert.get("metadata")
    if isinstance(metadata, dict):
        for key in ("hostile_count", "target_count", "count"):
            value = metadata.get(key)
            if value not in {None, ""}:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                return max(1, number)

    names = alert.get("names")
    if isinstance(names, list):
        return max(1, len([name for name in names if str(name or "").strip()]))
    return 1


def summarize_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Return the compact alert summary consumed by the overlay."""
    return {
        "id": str(alert.get("id") or "").strip(),
        "system_name": alert_system_name(alert),
        "hostile_count": alert_hostile_count(alert),
        "created_at": str(alert.get("created_at") or alert.get("seen_at") or ""),
        "source_observation_id": str(alert.get("source_observation_id") or "").strip(),
        "active_intel_id": str(alert.get("active_intel_id") or "").strip(),
        "active": bool(alert.get("active", True)),
        **({"freshness": "unknown"} if alert.get("freshness") == "unknown" else {}),
    }


def aggregate_alert_summaries(
    summaries: list[dict[str, Any]],
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """Keep one current row per system in discovery order."""
    by_system: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in summaries:
        system = str(item.get("system_name") or "Unknown").strip() or "Unknown"
        system_key = system.casefold()
        if item.get("freshness") == "unknown":
            by_system.pop(system_key, None)
            continue
        active = bool(item.get("active", True))
        raw_hostile_count = item.get("hostile_count")
        try:
            hostile_count = 1 if raw_hostile_count is None else int(raw_hostile_count)
        except (TypeError, ValueError):
            hostile_count = 0
        hostile_count = max(0, hostile_count)
        active = active and hostile_count > 0
        existing = by_system.get(system_key)
        if existing is None:
            by_system[system_key] = {
                "system_name": system,
                "hostile_count": hostile_count if active else 0,
                "active_hostile_count": hostile_count if active else 0,
                "created_at": str(item.get("created_at") or ""),
                "active": active,
                **({"freshness": "unknown"} if item.get("freshness") == "unknown" else {}),
            }
        else:
            existing["hostile_count"] = hostile_count if active else 0
            existing["active_hostile_count"] = hostile_count if active else 0
            existing["active"] = active
            existing.pop("freshness", None)
            if item.get("freshness") == "unknown":
                existing["freshness"] = "unknown"
            if not existing.get("created_at"):
                existing["created_at"] = str(item.get("created_at") or "")
    ordered = list(by_system.values())
    if max_rows is not None:
        return ordered[:max(1, max_rows)]
    return ordered


def format_alert_time(value: Any) -> str:
    """Return a compact local display time for an alert timestamp."""
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text[:5] if len(text) >= 5 else text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%H:%M")


def alert_identity_keys(alert: dict[str, Any]) -> set[str]:
    """Return stable keys used to match alert rows across bootstrap refreshes."""
    keys = set()
    for field in ("id", "source_observation_id", "active_intel_id"):
        value = str(alert.get(field) or "").strip()
        if value:
            keys.add(f"{field}:{value}")
    return keys


def active_alert_keys_from_bootstrap(bootstrap: dict[str, Any]) -> set[str]:
    """Return alert identity keys that are currently active server-side."""
    alerts = bootstrap.get("alerts")
    if not isinstance(alerts, list):
        return set()
    keys = set()
    for alert in alerts:
        if isinstance(alert, dict):
            keys.update(alert_identity_keys(alert))
    return keys


def update_alert_summaries_active(
    summaries: list[dict[str, Any]],
    active_keys: set[str],
) -> list[dict[str, Any]]:
    """Mark existing alert summaries active or inactive from server state."""
    updated = []
    for summary in summaries:
        item = dict(summary)
        keys = alert_identity_keys(item)
        item["active"] = bool(keys and keys.intersection(active_keys))
        updated.append(item)
    return updated


def prune_inactive_alert_summaries(
    summaries: list[dict[str, Any]],
    *,
    now: float | None = None,
    retention_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """Keep all known alert systems for the current client session."""
    _ = now, retention_seconds
    return [dict(summary) for summary in summaries]


def unavailable_systems_from_bootstrap(bootstrap: dict[str, Any]) -> set[str]:
    """Recognize invalidation in both current and legacy bootstrap projections."""
    map_payload = bootstrap.get("map")
    map_systems = map_payload.get("systems") if isinstance(map_payload, dict) else []
    unavailable: set[str] = set()
    for rows in (map_systems, bootstrap.get("active_intel"), bootstrap.get("alerts")):
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            if item.get("freshness") == "unknown" or metadata.get("freshness") == "unknown":
                system = str(item.get("system_name") or item.get("name") or "").strip()
                if system:
                    unavailable.add(system.casefold())
    return unavailable


def sync_alert_summaries_from_bootstrap(
    summaries: list[dict[str, Any]],
    bootstrap: dict[str, Any],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Sync live counts and retain green tiles only for monitored systems."""
    _ = now
    monitoring_systems = monitored_system_names(bootstrap.get("clients"))
    monitoring_keys = {system.casefold() for system in monitoring_systems}
    map_payload = bootstrap.get("map")
    map_systems = map_payload.get("systems") if isinstance(map_payload, dict) else None
    unavailable = unavailable_systems_from_bootstrap(bootstrap)
    monitoring_systems = [system for system in monitoring_systems if system.casefold() not in unavailable]
    monitoring_keys.difference_update(unavailable)
    if not isinstance(map_systems, list):
        active_keys = active_alert_keys_from_bootstrap(bootstrap)
        updated = update_alert_summaries_active(
            [item for item in summaries
             if str(item.get("system_name") or "").strip().casefold() not in unavailable
             and item.get("freshness") != "unknown"],
            active_keys,
        )
        for item in updated:
            if item.get("active"):
                item["active_hostile_count"] = item.get("hostile_count", 0)
            else:
                item["hostile_count"] = 0
                item["active_hostile_count"] = 0
        updated = [
            item
            for item in updated
            if bool(item.get("active"))
            or str(item.get("system_name") or "Unknown").casefold()
            in monitoring_keys
        ]
        known_systems = {
            str(item.get("system_name") or "Unknown").casefold()
            for item in updated
        }
        for system in monitoring_systems:
            if system.casefold() in known_systems:
                continue
            updated.append(
                {
                    "system_name": system,
                    "hostile_count": 0,
                    "active_hostile_count": 0,
                    "created_at": "",
                    "active": False,
                }
            )
        return updated

    previous_by_system = {
        str(item.get("system_name") or "Unknown").casefold(): item
        for item in summaries
    }
    first_seen_by_system: dict[str, str] = {}
    active_items = bootstrap.get("active_intel")
    active_items = active_items if isinstance(active_items, list) else []
    for item in active_items:
        if not isinstance(item, dict) or not bool(item.get("active", True)):
            continue
        system = str(item.get("system_name") or "").strip()
        first_seen = str(
            item.get("first_seen_at") or item.get("last_seen_at") or ""
        ).strip()
        if not system or not first_seen:
            continue
        system_key = system.casefold()
        existing = first_seen_by_system.get(system_key)
        if existing is None or first_seen < existing:
            first_seen_by_system[system_key] = first_seen

    current_by_system: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in map_systems:
        if not isinstance(item, dict):
            continue
        system = str(item.get("name") or item.get("system_name") or "").strip()
        try:
            hostile_count = int(item.get("hostile_count") or 0)
        except (TypeError, ValueError):
            hostile_count = 0
        if not system or hostile_count <= 0 or system.casefold() in unavailable:
            continue
        system_key = system.casefold()
        previous = previous_by_system.get(system_key, {})
        current_by_system[system_key] = {
            "system_name": system,
            "hostile_count": hostile_count,
            "active_hostile_count": hostile_count,
            "created_at": first_seen_by_system.get(system_key)
            or str(previous.get("created_at") or item.get("latest_seen") or ""),
            "active": True,
        }

    mapped_systems = set(current_by_system)
    hostile_active_ids = {
        str(alert.get("active_intel_id") or "").strip()
        for alert in bootstrap.get("alerts", [])
        if isinstance(alert, dict) and alert.get("active_intel_id")
    }
    for item in active_items:
        if not isinstance(item, dict) or not bool(item.get("active", True)):
            continue
        active_id = str(item.get("id") or "").strip()
        system = str(item.get("system_name") or "").strip()
        system_key = system.casefold()
        if not active_id or active_id not in hostile_active_ids or not system or system_key in unavailable:
            continue
        if system_key in mapped_systems:
            continue
        metadata = (
            item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        )
        try:
            hostile_count = int(metadata.get("hostile_count") or 1)
        except (TypeError, ValueError):
            hostile_count = 1
        count = max(1, hostile_count)
        existing = current_by_system.get(system_key)
        if existing is not None:
            existing["hostile_count"] = int(existing["hostile_count"]) + count
            existing["active_hostile_count"] = int(
                existing["active_hostile_count"]
            ) + count
            continue
        previous = previous_by_system.get(system_key, {})
        current_by_system[system_key] = {
            "system_name": system,
            "hostile_count": count,
            "active_hostile_count": count,
            "created_at": first_seen_by_system.get(system_key)
            or str(previous.get("created_at") or item.get("last_seen_at") or ""),
            "active": True,
        }

    for system in monitoring_systems:
        system_key = system.casefold()
        if system_key in current_by_system:
            continue
        previous = previous_by_system.get(system_key, {})
        current_by_system[system_key] = {
            "system_name": system,
            "hostile_count": 0,
            "active_hostile_count": 0,
            "created_at": str(previous.get("created_at") or ""),
            "active": False,
        }

    ordered: list[dict[str, Any]] = []
    for item in summaries:
        system = str(item.get("system_name") or "Unknown")
        current = current_by_system.pop(system.casefold(), None)
        if current is not None:
            ordered.append(current)
    ordered.extend(
        sorted(
            current_by_system.values(),
            key=lambda item: str(item.get("created_at") or ""),
        )
    )
    return ordered


def monitored_accounts_from_bootstrap(bootstrap: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract selectable online detector accounts and their current systems."""
    clients = bootstrap.get("clients") if isinstance(bootstrap, dict) else None
    heartbeats = clients.get("heartbeats") if isinstance(clients, dict) else None
    if not isinstance(heartbeats, list):
        return []
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for heartbeat in heartbeats:
        if not isinstance(heartbeat, dict):
            continue
        if str(heartbeat.get("client_type") or "") != "detector_client":
            continue
        heartbeat_health = str(
            heartbeat.get("health_status")
            or ("online" if heartbeat.get("online") else "removed")
        ).strip().casefold()
        if heartbeat_health != "online":
            continue
        details = heartbeat.get("details")
        if not isinstance(details, dict) or not bool(details.get("monitoring")):
            continue
        targets = details.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [details]
        for target in targets:
            if not isinstance(target, dict) or not bool(target.get("monitoring", True)):
                continue
            health_status = heartbeat_health
            if target.get("capture_online") is False:
                continue
            client_id = str(target.get("client_id") or heartbeat.get("client_id") or "").strip()
            character_name = str(target.get("character_name") or "").strip()
            source_instance = str(
                target.get("source_instance") or target.get("window_title") or ""
            ).strip()
            system_name = str(
                target.get("system_name")
                or target.get("system")
                or details.get("system_name")
                or details.get("system")
                or ""
            ).strip()
            if not system_name or system_name.casefold() == "unknown":
                continue
            key = "|".join((client_id, character_name, source_instance)).casefold()
            if key in seen:
                continue
            seen.add(key)
            label = character_name or source_instance or client_id or system_name
            try:
                hostile_count = max(
                    0,
                    int(target.get("hostile_icon_count") or 0),
                )
            except (TypeError, ValueError):
                hostile_count = 0
            accounts.append(
                {
                    "key": key,
                    "label": label,
                    "character_name": character_name,
                    "client_id": client_id,
                    "system_name": system_name,
                    "system_id": target.get("system_id"),
                    "monitoring": True,
                    "health_status": health_status,
                    "hostile_count": hostile_count,
                }
            )
    accounts.sort(
        key=lambda item: (
            str(item.get("character_name") or item.get("label") or "").casefold(),
            str(item.get("client_id") or "").casefold(),
            str(item.get("key") or "").casefold(),
        )
    )
    return accounts


def local_accounts_from_windows(
    windows: list[dict[str, Any]],
    systems_by_character: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build selectable local accounts from every currently running EVE window."""
    systems = systems_by_character or {}
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for window in windows:
        title = str(window.get("title") or "").strip()
        prefix, separator, character_name = title.partition(" - ")
        if not separator or prefix.strip().casefold() != "eve":
            character_name = str(window.get("character_name") or "").strip()
        else:
            character_name = character_name.strip()
        if not character_name:
            continue
        identity = character_name.casefold()
        key = f"local-account:{identity}"
        if key in seen:
            continue
        seen.add(key)
        system_name = str(systems.get(character_name.casefold()) or "Unknown").strip()
        accounts.append(
            {
                "key": key,
                "label": character_name or title or "EVE 窗口",
                "character_name": character_name,
                "source_instance": title,
                "window_title": title,
                "window_hwnd": window.get("hwnd"),
                "system_name": system_name or "Unknown",
                "system_id": None,
                "local": True,
                # Local windows are account locations even when monitoring is
                # stopped. The server heartbeat is the source of truth for
                # whether this account currently has an active monitor.
                "monitoring": False,
            }
        )
    return accounts


def _account_is_monitoring(account: dict[str, Any]) -> bool:
    """Return whether a map account represents an active monitoring target."""
    # Older payloads did not carry this field. They only exposed remote
    # accounts while monitored, and local fixtures represented monitored
    # accounts, so treat a missing value as active for compatibility.
    return bool(account.get("monitoring", True))


def _account_health_status(account: dict[str, Any]) -> str:
    """Return the normalized monitoring health state for a map account."""
    if not _account_is_monitoring(account):
        return "inactive"
    status = str(account.get("health_status") or "online").strip().casefold()
    return status if status in {"online", "degraded", "offline"} else "offline"


def merge_map_accounts(
    local_accounts: list[dict[str, Any]],
    remote_accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge local windows with matching server accounts without duplicates."""
    remote_by_character: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, account in enumerate(remote_accounts):
        character = str(account.get("character_name") or "").strip().casefold()
        if character:
            remote_by_character.setdefault(character, []).append((index, account))

    merged: list[dict[str, Any]] = []
    consumed_remote: set[int] = set()
    for local in local_accounts:
        item = dict(local)
        character = str(item.get("character_name") or "").strip().casefold()
        matches = remote_by_character.get(character, [])
        match = next(
            ((index, remote) for index, remote in matches if index not in consumed_remote),
            matches[0] if matches else None,
        )
        if match is not None:
            index, remote = match
            consumed_remote.add(index)
            local_system = str(item.get("system_name") or "").strip()
            remote_system = str(remote.get("system_name") or "").strip()
            if local_system.casefold() == remote_system.casefold():
                item["system_id"] = remote.get("system_id")
                item["client_id"] = remote.get("client_id")
                item["monitoring"] = _account_is_monitoring(remote)
                item["health_status"] = _account_health_status(remote)
                item["hostile_count"] = remote.get("hostile_count", 0)
        merged.append(item)

    merged.extend(
        dict(account)
        for index, account in enumerate(remote_accounts)
        if index not in consumed_remote and not bool(account.get("local"))
    )
    return merged


def is_local_monitored_account(client_id: Any, alert_client_id: Any) -> bool:
    """Return whether a detector target belongs to this installation."""
    detector_id = str(client_id or "").strip().casefold()
    current_id = str(alert_client_id or "").strip().casefold()
    _, separator, installation_id = current_id.partition(":")
    if not separator or not installation_id:
        return False
    detector_prefix = f"detector-client:{installation_id}"
    return detector_id == detector_prefix or detector_id.startswith(
        f"{detector_prefix}:"
    )


class MultiSelectMenu(QMenu):
    """Keep the popup open while alert targets are toggled."""

    def mouseReleaseEvent(self, event) -> None:
        action = self.actionAt(event.position().toPoint())
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


class LocalStarMapWidget(QWidget):
    """Small, dependency-free renderer for the selected map neighborhood."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._payload: dict[str, Any] = {}
        self._accounts: list[dict[str, Any]] = []
        self._alerts: list[dict[str, Any]] = []
        self._node_tooltips: list[tuple[QRect, str]] = []
        self._active_tooltip = ""
        self._message = "选择账号加载局部星图"
        self.setMinimumHeight(150)
        self.setMouseTracking(True)

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload or {})
        self._message = ""
        self.update()

    def set_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._accounts = [dict(item) for item in accounts]
        self.update()

    def set_alerts(self, alerts: list[dict[str, Any]]) -> None:
        self._alerts = [dict(item) for item in alerts if item.get("freshness") != "unknown"]
        self.update()

    def set_message(self, message: str) -> None:
        self._message = str(message or "")
        self.update()

    @staticmethod
    def _star_path(x: float, y: float, radius: float = 7.0) -> QPainterPath:
        path = QPainterPath()
        inner_radius = radius * 0.46
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            point_radius = radius if index % 2 == 0 else inner_radius
            point_x = x + math.cos(angle) * point_radius
            point_y = y + math.sin(angle) * point_radius
            if index == 0:
                path.moveTo(point_x, point_y)
            else:
                path.lineTo(point_x, point_y)
        path.closeSubpath()
        return path

    @staticmethod
    def _node_annotation(
        system_name: str,
        accounts: list[dict[str, Any]],
        hostile_count: int,
    ) -> tuple[str, str]:
        """Build the preferred account identity and full hover details."""
        selected = [item for item in accounts if bool(item.get("selected", True))]
        if not accounts and hostile_count <= 0:
            return "", ""

        local_selected = [item for item in selected if bool(item.get("local"))]
        local_accounts = [item for item in accounts if bool(item.get("local"))]
        primary_candidates = local_selected or local_accounts
        primary = primary_candidates[0] if primary_candidates else None
        label = (
            str(
                primary.get("label")
                or primary.get("character_name")
                or system_name
            ).strip()
            if primary is not None
            else system_name
        )

        primary_key = str(primary.get("key") or "") if primary is not None else ""
        details = [f"星系：{system_name}"]
        if primary is not None:
            account_kind = "本地账号" if bool(primary.get("local")) else "监控账号"
            details.append(f"{account_kind}：{label}")
            if _account_is_monitoring(primary):
                details.append(
                    "监控："
                    + {
                        "online": "正常",
                        "degraded": "连接异常",
                        "offline": "节点离线",
                    }.get(_account_health_status(primary), "节点离线")
                )
        remote_accounts = [
            item
            for item in accounts
            if not bool(item.get("local")) and _account_is_monitoring(item)
        ]
        if remote_accounts:
            statuses = {_account_health_status(item) for item in remote_accounts}
            if "online" in statuses:
                node_status = "正常"
            elif "degraded" in statuses:
                node_status = "连接异常"
            else:
                node_status = "离线"
            details.append(f"监控节点：{node_status}")
            details.append(f"节点数量：{len(remote_accounts)}")
        remaining = [
            str(item.get("label") or item.get("character_name") or "").strip()
            for item in accounts
            if str(item.get("key") or "") != primary_key
            and bool(item.get("local"))
        ]
        remaining = list(dict.fromkeys(item for item in remaining if item))
        if remaining:
            details.append(f"其他账号：{'、'.join(remaining)}")
        if hostile_count > 0:
            details.append(f"来敌：{hostile_count} 人")
        return label or system_name, "\n".join(details)

    def paintEvent(self, event) -> None:
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), QColor(5, 14, 22, 165))
        self._node_tooltips = []
        systems = [
            item
            for item in self._payload.get("systems", [])
            if isinstance(item, dict)
        ]
        links = [
            item
            for item in self._payload.get("links", [])
            if isinstance(item, dict)
        ]
        if not systems:
            painter.setPen(QColor("#93a4b4"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                self._message or "暂无局部星图数据",
            )
            return
        names = {str(item.get("name") or "").strip() for item in systems}
        positions = {
            str(item.get("name") or "").strip(): (
                float(item.get("x") or 0),
                float(item.get("y") or 0),
            )
            for item in systems
            if str(item.get("name") or "").strip()
        }
        if not positions:
            return
        min_x = min(point[0] for point in positions.values())
        max_x = max(point[0] for point in positions.values())
        min_y = min(point[1] for point in positions.values())
        max_y = max(point[1] for point in positions.values())
        padding = 18.0
        raw_width = max_x - min_x
        raw_height = max_y - min_y
        width = max(1.0, raw_width)
        height = max(1.0, raw_height)
        scale = min(
            (self.width() - padding * 2) / width,
            (self.height() - padding * 2) / height,
        )
        scale = max(0.1, scale)
        offset_x = (self.width() - raw_width * scale) / 2
        offset_y = (self.height() - raw_height * scale) / 2

        def point(name: str) -> tuple[float, float]:
            x, y = positions[name]
            return (
                offset_x + (x - min_x) * scale,
                offset_y + (max_y - y) * scale,
            )

        painter.setPen(QPen(QColor(66, 103, 125, 170), 1))
        for link in links:
            source = str(link.get("from") or "").strip()
            target = str(link.get("to") or "").strip()
            if source in names and target in names:
                source_x, source_y = point(source)
                target_x, target_y = point(target)
                painter.drawLine(
                    int(source_x),
                    int(source_y),
                    int(target_x),
                    int(target_y),
                )

        accounts_by_system: dict[str, list[dict[str, Any]]] = {}
        for account in self._accounts:
            system_key = str(account.get("system_name") or "").strip().casefold()
            if system_key:
                accounts_by_system.setdefault(system_key, []).append(account)
        centers = {
            system_key
            for system_key, accounts in accounts_by_system.items()
            if any(bool(item.get("selected", True)) for item in accounts)
        }
        hostile_counts: dict[str, int] = {}
        unknown_systems = {str(alert.get("system_name") or "").strip().casefold()
                           for alert in self._alerts if alert.get("freshness") == "unknown"}
        for alert in self._alerts:
            if not bool(alert.get("active", True)):
                continue
            name = str(alert.get("system_name") or "").strip().casefold()
            try:
                count = int(alert.get("active_hostile_count", alert.get("hostile_count", 0)) or 0)
            except (TypeError, ValueError):
                count = 0
            hostile_counts[name] = max(hostile_counts.get(name, 0), count)
        labels: list[tuple[bool, float, float, str]] = []
        for name in sorted(names):
            x, y = point(name)
            key = name.casefold()
            hostile = hostile_counts.get(key, 0) > 0
            monitoring = any(
                _account_health_status(item) == "online"
                for item in accounts_by_system.get(key, [])
            )
            degraded = any(
                _account_health_status(item) == "degraded"
                for item in accounts_by_system.get(key, [])
            )
            offline = any(
                _account_health_status(item) == "offline"
                for item in accounts_by_system.get(key, [])
            )
            focused = key in centers
            local_focused = any(
                bool(item.get("local"))
                and bool(item.get("selected", True))
                for item in accounts_by_system.get(key, [])
            )
            radius = 12 if hostile else 10 if focused else 9 if (monitoring or degraded or offline) else 4
            if hostile:
                outer_radius = 11
                painter.setPen(QPen(QColor(255, 107, 115, 225), 2))
                painter.setBrush(QBrush(QColor(104, 19, 28, 210)))
                painter.drawEllipse(
                    int(x - outer_radius),
                    int(y - outer_radius),
                    outer_radius * 2,
                    outer_radius * 2,
                )
            if monitoring and not hostile:
                inner_radius = 8
                painter.setPen(QPen(QColor(111, 240, 213, 235), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(
                    int(x - inner_radius),
                    int(y - inner_radius),
                    inner_radius * 2,
                    inner_radius * 2,
                )
            elif degraded and not hostile:
                painter.setPen(QPen(QColor("#f6c760"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(int(x - 8), int(y - 8), 16, 16)
            elif offline and not hostile:
                painter.setPen(QPen(QColor("#8b95a5"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(int(x - 8), int(y - 8), 16, 16)

            if local_focused:
                star_color = QColor("#ff5965") if hostile else QColor("#ffd166")
                painter.setPen(QPen(star_color.lighter(125), 1))
                painter.setBrush(QBrush(star_color))
                painter.drawPath(self._star_path(x, y))
            elif hostile:
                painter.save()
                painter.translate(x, y)
                painter.rotate(45)
                painter.setPen(QPen(QColor("#ff9aa0"), 1))
                painter.setBrush(QBrush(QColor("#ff5965")))
                painter.drawRect(-5, -5, 10, 10)
                painter.restore()
            elif monitoring:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor("#45d4bd")))
                painter.drawEllipse(int(x - 4), int(y - 4), 8, 8)
            elif degraded:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor("#f6c760")))
                painter.drawEllipse(int(x - 4), int(y - 4), 8, 8)
            elif offline:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor("#8b95a5")))
                painter.drawEllipse(int(x - 4), int(y - 4), 8, 8)
            else:
                color = QColor("#6d8394")
                painter.setPen(QPen(color, 1))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(int(x - 4), int(y - 4), 8, 8)
            if key in unknown_systems:
                painter.setPen(QColor("#f6c760"))
                painter.drawText(int(x + 13), int(y + 4), "上次")
            if hostile:
                labels.append((True, x, y, f"{name} · 敌 {hostile_counts[key]}"))
            elif local_focused:
                labels.append((False, x, y, name))
            if not (local_focused or monitoring or degraded or offline or hostile or key in unknown_systems):
                continue

            _label, tooltip = self._node_annotation(
                name,
                accounts_by_system.get(key, []),
                hostile_counts.get(key, 0),
            )
            if key in unknown_systems:
                tooltip += "\n采集状态未知，显示上次确认敌情"
            hit_radius = max(12, radius)
            node_rect = QRect(
                int(x - hit_radius),
                int(y - hit_radius),
                hit_radius * 2,
                hit_radius * 2,
            )
            self._node_tooltips.append((node_rect, tooltip))

        # Keep the former list's essential details visible on the map.
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        occupied = [rect for rect, _ in self._node_tooltips]
        for hostile, x, y, label in sorted(labels, key=lambda item: not item[0]):
            width = metrics.horizontalAdvance(label) + 8
            height = metrics.height() + 4
            for left in (int(x) + 13, int(x) - width - 13):
                rect = QRect(left, int(y) - height // 2, width, height)
                if not self.rect().contains(rect) or any(rect.intersects(item) for item in occupied):
                    continue
                occupied.append(rect)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(12, 26, 37, 240))
                painter.drawRoundedRect(rect, 3, 3)
                painter.setPen(QColor("#ffb3b8" if hostile else "#ffe3a0"))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
                break

    def mouseMoveEvent(self, event) -> None:
        tooltip = next(
            (
                text
                for rect, text in self._node_tooltips
                if rect.contains(event.position().toPoint())
            ),
            "",
        )
        if tooltip and tooltip != self._active_tooltip:
            self._active_tooltip = tooltip
            QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
        elif not tooltip and self._active_tooltip:
            self._active_tooltip = ""
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._active_tooltip = ""
        QToolTip.hideText()
        super().leaveEvent(event)


def build_heartbeat_details(
    last_action: str,
    last_error: str = "",
    client_version: str = "",
    host: str = "",
    last_success_at: str = "",
    connection_id: str = "",
    alert_scope: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Return the alert-client heartbeat details for status views."""
    details: dict[str, object] = {
        "mode": "events",
        "transport": "events",
        "ack_capability": ALERT_ACK_PROTOCOL,
        "popup": True,
        "overlay": True,
        "details": False,
    }
    action = str(last_action or "").strip()
    if action:
        details["last_action"] = action
    error = summarize_heartbeat_error(last_error)
    if error:
        details["last_error"] = error
    if client_version:
        details["client_version"] = client_version
    if host:
        details["host"] = host
    if last_success_at:
        details["last_success_at"] = last_success_at
    if connection_id:
        details["connection_id"] = connection_id
    if alert_scope is not None:
        details["alert_scope"] = copy.deepcopy(alert_scope)
    return details


def play_alert_sound(
    volume: float = 1.0,
    sound_path: str | Path | None = None,
) -> None:
    """Play a configured WAV alert sound, falling back to the bundled resource."""
    bundled_path = Path(__file__).parent.parent / "resources" / "alert.wav"
    candidate = Path(sound_path).expanduser() if str(sound_path or "").strip() else None
    sound_path = (
        candidate
        if candidate is not None
        and candidate.is_file()
        and candidate.suffix.casefold() == ".wav"
        else bundled_path
    )
    if not sound_path.exists():
        return
    try:
        sound = QSoundEffect()
        sound.setSource(QUrl.fromLocalFile(str(sound_path.resolve())))
        sound.setVolume(max(0.0, min(1.0, float(volume))))
        sound.play()
        AlertOverlay.ACTIVE_SOUNDS.append(sound)
        QTimer.singleShot(5000, lambda: _forget_sound(sound))
    except Exception as exc:
        logger.warning("Failed to play alert sound: %s", exc)


def _forget_sound(sound: QSoundEffect) -> None:
    try:
        AlertOverlay.ACTIVE_SOUNDS.remove(sound)
    except ValueError:
        pass


class AlertOverlay(QWidget):
    """Always-on-top compact alert overlay."""

    ACTIVE_SOUNDS: list[QSoundEffect] = []
    map_options_changed = pyqtSignal(list, int)
    sound_stop_requested = pyqtSignal()
    geometry_changed = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._status = QLabel("连接中")
        self._status.setObjectName("statusLabel")
        self._map_widget: LocalStarMapWidget | None = None
        self._account_button: QPushButton | None = None
        self._account_menu: QMenu | None = None
        self._account_actions: list[QAction] = []
        self._sound_stop_button: QPushButton | None = None
        self._hops_spinbox: QSpinBox | None = None
        self._map_accounts: list[dict[str, Any]] = []
        self._map_alerts: list[dict[str, Any]] = []
        self._map_selected_account_keys: set[str] | None = None
        self._anchor_rect: dict[str, Any] | None = None
        self._drag_position: QPoint | None = None
        self._resize_edges = 0
        self._resize_start_geometry: QRect | None = None
        self._resize_start_position: QPoint | None = None
        self._user_positioned = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("EVE Sentry Alert")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(OVERLAY_MIN_WIDTH, OVERLAY_MIN_HEIGHT)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setMouseTracking(True)
        self.installEventFilter(self)

        frame = QFrame()
        frame.setObjectName("overlayFrame")
        frame.setMouseTracking(True)
        frame.installEventFilter(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(frame)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 5, 8, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(5)

        account_button = QPushButton("账号 0/0")
        account_button.setObjectName("mapAccountButton")
        account_button.setMinimumHeight(25)
        account_button.setEnabled(False)
        account_menu = MultiSelectMenu(account_button)
        account_menu.setObjectName("mapAccountMenu")
        account_button.setMenu(account_menu)
        header.addWidget(account_button)

        range_label = QLabel("预警范围")
        range_label.setObjectName("mapToolbarLabel")
        for label in (range_label, self._status):
            label.setMouseTracking(True)
            label.installEventFilter(self)
        hops_spinbox = QSpinBox()
        hops_spinbox.setObjectName("mapHopsSpinBox")
        hops_spinbox.setRange(0, MAX_MAP_NEIGHBORHOOD_HOPS)
        hops_spinbox.setValue(DEFAULT_ALERT_RANGE_HOPS)
        hops_spinbox.setSuffix(" 跳")
        hops_spinbox.setMinimumHeight(25)
        hops_spinbox.setAccessibleName("预警范围")
        hops_spinbox.setToolTip("只提醒所选角色指定跳数内的星系，0 跳仅提醒角色所在星系")
        hops_spinbox.valueChanged.connect(lambda _value: self._emit_map_options())
        range_label.setBuddy(hops_spinbox)
        header.addWidget(range_label)
        header.addWidget(hops_spinbox)
        header.addStretch(1)

        sound_stop_button = QPushButton("停止本轮")
        sound_stop_button.setObjectName("alertSoundStopButton")
        sound_stop_button.setMinimumHeight(25)
        sound_stop_button.setToolTip("停止本轮持续告警声音，直到范围内不再有活动预警")
        sound_stop_button.clicked.connect(self.sound_stop_requested.emit)
        sound_stop_button.hide()
        header.addWidget(sound_stop_button)
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._status)
        layout.addLayout(header)

        map_widget = LocalStarMapWidget()
        layout.addWidget(map_widget, 1)
        self._map_widget = map_widget
        self._account_button = account_button
        self._account_menu = account_menu
        self._sound_stop_button = sound_stop_button
        self._hops_spinbox = hops_spinbox
        self.resize(340, 230)

        self.setStyleSheet(
            """
            QFrame#overlayFrame {
                background: rgba(4, 12, 18, 155);
                border: 1px solid rgba(91, 190, 211, 165);
                border-radius: 8px;
            }
            QLabel#statusLabel {
                color: #9fb7c3;
                font-size: 11px;
                font-weight: 600;
                padding-left: 4px;
            }
            QPushButton {
                color: #b9d1dd;
                background: rgba(17, 36, 48, 210);
                border: 1px solid rgba(92, 213, 238, 100);
                border-radius: 3px;
                padding: 1px 5px;
            }
            QPushButton:hover {
                background: rgba(28, 61, 76, 230);
            }
            QPushButton#alertSoundStopButton {
                color: #ffe9e5;
                background: rgba(104, 39, 39, 220);
                border: 1px solid rgba(239, 116, 109, 150);
                padding: 1px 8px;
            }
            QPushButton#alertSoundStopButton:hover {
                background: rgba(139, 49, 48, 235);
            }
            QLabel#mapToolbarLabel {
                color: #91a9b6;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#mapAccountButton {
                color: #d8edf3;
                background: rgba(18, 43, 55, 185);
                border: 1px solid rgba(92, 181, 202, 110);
                border-radius: 3px;
                padding: 1px 7px;
                text-align: left;
                font-weight: 600;
            }
            QPushButton#mapAccountButton:hover {
                background: rgba(29, 66, 81, 235);
            }
            QSpinBox#mapHopsSpinBox {
                color: #f1fbfd;
                background: rgba(17, 40, 51, 185);
                border: 1px solid rgba(91, 181, 202, 120);
                border-radius: 3px;
                padding: 0 3px;
            }
            QMenu#mapAccountMenu {
                color: #dce8ef;
                background: rgba(6, 19, 28, 220);
                border: 1px solid rgba(92, 213, 238, 80);
                border-radius: 3px;
            }
            QMenu#mapAccountMenu::item {
                padding: 5px 20px 5px 8px;
            }
            QMenu#mapAccountMenu::item:selected {
                background: rgba(28, 61, 76, 230);
            }
            """
        )

    def _emit_map_options(self) -> None:
        selected = self._selected_map_account_keys()
        self._update_map_account_button()
        hops = self._hops_spinbox.value() if self._hops_spinbox is not None else DEFAULT_ALERT_RANGE_HOPS
        if self._map_widget is not None:
            selected_keys = set(selected)
            self._map_widget.set_accounts(
                [
                    {
                        **item,
                        "selected": str(item.get("key") or "") in selected_keys,
                    }
                    for item in self._map_accounts
                ]
            )
        self.map_options_changed.emit(selected, hops)

    def _on_map_account_toggled(self, _checked: bool) -> None:
        current_keys = {
            str(action.data() or "").strip()
            for action in self._account_actions
            if str(action.data() or "").strip()
        }
        selected_keys = set(self._selected_map_account_keys())
        remembered_keys = (
            set(self._map_selected_account_keys)
            if self._map_selected_account_keys is not None
            else set(current_keys)
        )
        self._map_selected_account_keys = (
            remembered_keys - current_keys
        ) | selected_keys
        self._emit_map_options()

    def _selected_map_account_keys(self) -> list[str]:
        return [
            str(action.data() or "")
            for action in self._account_actions
            if action.isChecked()
        ]

    def _update_map_account_button(self) -> None:
        if self._account_button is None:
            return
        selected_count = len(self._selected_map_account_keys())
        total_count = len(self._account_actions)
        self._account_button.setText(f"账号 {selected_count}/{total_count}")
        self._account_button.setEnabled(total_count > 0)

    def map_selection(self) -> tuple[list[str], int]:
        selected = self._selected_map_account_keys()
        hops = self._hops_spinbox.value() if self._hops_spinbox is not None else DEFAULT_ALERT_RANGE_HOPS
        return selected, hops

    def set_alert_range_hops(self, hops: int) -> None:
        if self._hops_spinbox is not None:
            self._hops_spinbox.blockSignals(True)
            self._hops_spinbox.setValue(hops)
            self._hops_spinbox.blockSignals(False)

    def map_selection_memory(self) -> list[str] | None:
        """Return explicit selection memory, retaining currently offline accounts."""
        if self._map_selected_account_keys is None:
            return None
        return sorted(self._map_selected_account_keys)

    def set_map_selection_memory(self, account_keys: list[str] | None) -> None:
        """Restore explicit selection memory before the account menu is populated."""
        if account_keys is None:
            self._map_selected_account_keys = None
            return
        self._map_selected_account_keys = {
            str(item or "").strip()
            for item in account_keys
            if str(item or "").strip()
        }

    def set_map_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._map_accounts = [dict(item) for item in accounts]
        if self._account_menu is None:
            return
        selectable_accounts = [
            item
            for item in self._map_accounts
            if bool(item.get("local", True))
        ]
        self._account_menu.clear()
        self._account_actions.clear()
        for account in selectable_accounts:
            key = str(account.get("key") or "")
            label = str(account.get("label") or account.get("system_name") or key)
            system = str(account.get("system_name") or "")
            location = system if system and system.casefold() != "unknown" else "定位中"
            text = f"{label} · {location}"
            action = self._account_menu.addAction(text)
            action.setData(key)
            action.setCheckable(True)
            action.setChecked(
                self._map_selected_account_keys is None
                or key in self._map_selected_account_keys
            )
            action.toggled.connect(self._on_map_account_toggled)
            self._account_actions.append(action)
        self._update_map_account_button()
        if self._map_widget is not None:
            selected, _ = self.map_selection()
            selected_keys = set(selected)
            self._map_widget.set_accounts(
                [
                    {
                        **item,
                        "selected": str(item.get("key") or "") in selected_keys,
                    }
                    for item in self._map_accounts
                ]
            )

    def set_map_payload(self, payload: dict[str, Any]) -> None:
        if self._map_widget is not None:
            self._map_widget.set_payload(payload)

    def set_map_alerts(self, alerts: list[dict[str, Any]]) -> None:
        self._map_alerts = [dict(item) for item in alerts]
        if self._map_widget is not None:
            self._map_widget.set_alerts(self._map_alerts)

    def set_map_message(self, message: str) -> None:
        if self._map_widget is not None:
            self._map_widget.set_message(message)

    def set_status(self, text: str, tone: str = "idle") -> None:
        color = {
            "ok": "#6ee7b7",
            "warn": "#f5c96b",
            "danger": "#ff8b7f",
            "idle": "#9fb7c3",
        }.get(tone, "#9fb7c3")
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color};")

    def set_sound_stop_available(self, available: bool) -> None:
        """Show or hide the one-click stop control for continuous alerts."""
        button = getattr(self, "_sound_stop_button", None)
        if button is not None:
            button.setVisible(bool(available))
            button.setEnabled(bool(available))

    def mousePressEvent(self, event) -> None:
        if self._handle_drag_event(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._handle_drag_event(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._handle_drag_event(event):
            return
        super().mouseReleaseEvent(event)

    def eventFilter(self, watched, event) -> bool:
        _ = watched
        if event.type() in {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
        }:
            return self._handle_drag_event(event)
        return False

    def _handle_drag_event(self, event) -> bool:
        global_position = event.globalPosition().toPoint()
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            resize_edges = self._resize_edges_at(global_position)
            if resize_edges:
                self._resize_edges = resize_edges
                self._resize_start_geometry = QRect(self.geometry())
                self._resize_start_position = global_position
                self._drag_position = None
                self._user_positioned = True
                event.accept()
                return True
            self._drag_position = (
                global_position - self.frameGeometry().topLeft()
            )
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseMove:
            if self._resize_edges and (
                event.buttons() & Qt.MouseButton.LeftButton
            ):
                self._resize_from_pointer(global_position)
                event.accept()
                return True
            if self._drag_position is None or not (
                event.buttons() & Qt.MouseButton.LeftButton
            ):
                edges = self._resize_edges_at(global_position)
                self.setCursor(self._cursor_for_resize_edges(edges))
                return bool(edges)
            self.move(global_position - self._drag_position)
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            if self._resize_edges:
                self._resize_edges = 0
                self._resize_start_geometry = None
                self._resize_start_position = None
                self.setCursor(
                    self._cursor_for_resize_edges(
                        self._resize_edges_at(global_position)
                    )
                )
                self.geometry_changed.emit(self.geometry_state())
                event.accept()
                return True
            if self._drag_position is not None:
                self._user_positioned = True
                self.geometry_changed.emit(self.geometry_state())
            self._drag_position = None
            event.accept()
            return True
        return False

    def _resize_edges_at(self, global_position: QPoint) -> int:
        geometry = self.frameGeometry()
        margin = OVERLAY_RESIZE_MARGIN
        edges = 0
        if abs(global_position.x() - geometry.left()) <= OVERLAY_HORIZONTAL_RESIZE_MARGIN:
            edges |= RESIZE_LEFT
        elif abs(global_position.x() - geometry.right()) <= OVERLAY_HORIZONTAL_RESIZE_MARGIN:
            edges |= RESIZE_RIGHT
        if abs(global_position.y() - geometry.top()) <= margin:
            edges |= RESIZE_TOP
        elif abs(global_position.y() - geometry.bottom()) <= margin:
            edges |= RESIZE_BOTTOM
        return edges

    @staticmethod
    def _cursor_for_resize_edges(edges: int) -> Qt.CursorShape:
        if edges in {RESIZE_LEFT | RESIZE_TOP, RESIZE_RIGHT | RESIZE_BOTTOM}:
            return Qt.CursorShape.SizeFDiagCursor
        if edges in {RESIZE_RIGHT | RESIZE_TOP, RESIZE_LEFT | RESIZE_BOTTOM}:
            return Qt.CursorShape.SizeBDiagCursor
        if edges & (RESIZE_LEFT | RESIZE_RIGHT):
            return Qt.CursorShape.SizeHorCursor
        if edges & (RESIZE_TOP | RESIZE_BOTTOM):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.SizeAllCursor

    def _resize_from_pointer(self, global_position: QPoint) -> None:
        start_geometry = self._resize_start_geometry
        start_position = self._resize_start_position
        if start_geometry is None or start_position is None:
            return
        delta = global_position - start_position
        geometry = QRect(start_geometry)
        minimum_width = self.minimumWidth()
        minimum_height = self.minimumHeight()
        if self._resize_edges & RESIZE_LEFT:
            geometry.setLeft(
                min(
                    start_geometry.left() + delta.x(),
                    start_geometry.right() - minimum_width + 1,
                )
            )
        elif self._resize_edges & RESIZE_RIGHT:
            geometry.setRight(
                max(
                    start_geometry.right() + delta.x(),
                    start_geometry.left() + minimum_width - 1,
                )
            )
        if self._resize_edges & RESIZE_TOP:
            geometry.setTop(
                min(
                    start_geometry.top() + delta.y(),
                    start_geometry.bottom() - minimum_height + 1,
                )
            )
        elif self._resize_edges & RESIZE_BOTTOM:
            geometry.setBottom(
                max(
                    start_geometry.bottom() + delta.y(),
                    start_geometry.top() + minimum_height - 1,
                )
            )
        self.setGeometry(geometry)

    def geometry_state(self) -> dict[str, int]:
        """Return a JSON-safe overlay rectangle."""
        geometry = self.geometry()
        return {
            "x": geometry.x(),
            "y": geometry.y(),
            "width": geometry.width(),
            "height": geometry.height(),
        }

    def restore_geometry_state(self, value: dict[str, Any] | None) -> bool:
        """Restore a saved rectangle when it still belongs to a visible screen."""
        if not isinstance(value, dict):
            return False
        try:
            saved = QRect(
                int(value["x"]),
                int(value["y"]),
                max(self.minimumWidth(), int(value["width"])),
                max(self.minimumHeight(), int(value["height"])),
            )
        except (KeyError, TypeError, ValueError):
            return False
        candidates = []
        for screen in QApplication.screens():
            intersection = saved.intersected(screen.availableGeometry())
            candidates.append((intersection.width() * intersection.height(), screen))
        if not candidates:
            return False
        area, screen = max(candidates, key=lambda item: item[0])
        if area <= 0:
            return False
        available = screen.availableGeometry()
        width = min(saved.width(), available.width())
        height = min(saved.height(), available.height())
        x = max(available.left(), min(saved.x(), available.right() - width + 1))
        y = max(available.top(), min(saved.y(), available.bottom() - height + 1))
        self.setGeometry(x, y, width, height)
        self._user_positioned = True
        return True

    def show_summaries(self, summaries: list[dict[str, Any]]) -> None:
        alerts = aggregate_alert_summaries([
            item for item in summaries
            if str(item.get("system_name") or "").strip().casefold() not in {"", "unknown"}
        ])
        for alert in alerts:
            del alert["created_at"]
        if alerts != self._map_alerts:
            self.set_map_alerts(alerts)

    def set_anchor_rect(self, rect: dict[str, Any] | None) -> None:
        """Anchor automatic placement to the display hosting an EVE window."""
        self._anchor_rect = dict(rect) if rect else None
        if not self._user_positioned:
            self.move_to_default_position()

    def _screen_for_anchor(self):
        anchor = self._anchor_rect or {}
        monitor_name = str(anchor.get("monitor") or "").strip().casefold()
        if monitor_name:
            for screen in QApplication.screens():
                if str(screen.name() or "").strip().casefold() == monitor_name:
                    return screen
        if anchor:
            center = QPoint(
                int(anchor.get("x") or 0) + int(anchor.get("w") or 0) // 2,
                int(anchor.get("y") or 0) + int(anchor.get("h") or 0) // 2,
            )
            screen = QApplication.screenAt(center)
            if screen is not None:
                return screen
        return QApplication.primaryScreen()

    def move_to_default_position(self) -> None:
        screen = self._screen_for_anchor()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        x = geometry.right() - self.width() - 28
        y = geometry.top() + 88
        max_x = geometry.right() - self.width() + 1
        max_y = geometry.bottom() - self.height() + 1
        self.move(
            max(geometry.left(), min(x, max_x)),
            max(geometry.top(), min(y, max_y)),
        )


class AlertEventWorker(QThread):
    """Background SSE consumer for server-side alert events."""

    alert_received = pyqtSignal(dict)
    safe_received = pyqtSignal(dict)
    bootstrap_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str, str)

    def __init__(
        self,
        server: str,
        state: AlertClientState,
        *,
        timeout: float = DEFAULT_EVENT_TIMEOUT,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY,
        client_id: str = "",
        api_key: str = "",
        api_factory: Callable[..., IntelApiClient] = IntelApiClient,
    ) -> None:
        super().__init__()
        self.server = server
        self.state = state
        self.timeout = max(1.0, float(timeout))
        self.heartbeat_interval = max(5.0, float(heartbeat_interval))
        self.reconnect_max_delay = max(1.0, float(reconnect_max_delay))
        self.client_id = client_id or persistent_client_id("alert")
        self.api_key = str(api_key or "").strip()
        self.api_factory = api_factory
        self.consumer = AlertEventConsumer(state)
        self._stop_requested = False
        self._last_heartbeat_at = 0.0
        self._last_heartbeat_scope_revision = ""
        self._alert_scope_lock = threading.Lock()
        self._alert_scope_signature: tuple[str, ...] = ()
        self._alert_scope = {"version": 1, "revision": str(uuid4()), "systems": []}
        self._last_success_at = ""
        self._runtime = resolve_runtime_identity()
        # A delivery is acknowledged only after the controller confirms that
        # the alert reached the UI.  The queue keeps HTTP work on this thread
        # while allowing the Qt UI thread to report that it handled an alert.
        self.connection_id = f"{self.client_id}:{uuid4().hex}"
        self._ack_queue: queue.Queue[dict[str, Any]] = queue.Queue()

    def set_alert_scope(self, system_names: list[str]) -> None:
        """Publish selected account centers from the UI without doing network I/O."""
        names: dict[str, str] = {}
        for value in system_names:
            name = str(value or "").strip()
            if name and name.casefold() != "unknown":
                names.setdefault(name.casefold(), name)
        signature = tuple(sorted(names))
        with self._alert_scope_lock:
            if signature == self._alert_scope_signature:
                return
            self._alert_scope_signature = signature
            # A->B->A must not reuse A's revision: the intervening selection
            # may happen before the worker sends its next heartbeat.
            self._alert_scope = {
                "version": 1,
                "revision": str(uuid4()),
                "systems": [{"name": names[key]} for key in signature],
            }

    def _alert_scope_snapshot(self) -> dict[str, Any]:
        with self._alert_scope_lock:
            return copy.deepcopy(self._alert_scope)

    def stop(self) -> None:
        """Request the worker to stop after the current network call returns."""
        self._stop_requested = True

    def queue_alert_ack(self, alert: dict[str, Any]) -> None:
        """Queue a UI-handled alert for a retryable, idempotent ACK."""
        if isinstance(alert, dict):
            self._ack_queue.put(dict(alert))

    def run(self) -> None:
        api = self.api_factory(
            self.server,
            # Heartbeats share this client with the SSE stream.  A three-second
            # request timeout is too aggressive when the server is briefly
            # busy (for example, while persisting OCR data), which makes a
            # healthy alert connection report a false timeout.  Keep the
            # request bounded so shutdown remains responsive, but allow the
            # same short load spikes tolerated by the detector client.
            timeout=min(15.0, max(5.0, self.timeout)),
            api_key=self.api_key,
        )
        backoff = 1.0
        # Track acknowledgements within a connection; they are not a request to
        # replay the gap when this live-only view opens another connection.
        resume_event_id = ""

        def remember_cursor(event_id: object) -> None:
            nonlocal resume_event_id
            candidate = str(event_id or "").strip()
            current_seq = _state_event_sequence(resume_event_id)
            candidate_seq = _state_event_sequence(candidate)
            if not candidate or (
                current_seq is not None
                and (candidate_seq is None or candidate_seq < current_seq)
            ):
                return
            resume_event_id = candidate
            self.state.save_last_event_id(candidate)

        while not self._stop_requested:
            # Bootstrap and its state:W watermark come from the same server
            # snapshot. Start there after errors AND normal EOF, then consume
            # seq > W on this stream instead of replaying an ended incursion.
            resume_event_id = ""
            retrying_after_error = False
            try:
                self._drain_alert_acks(api)
                connection_announced = False
                for event in api.iter_events(
                    timeout=self.timeout,
                    last_event_id=resume_event_id,
                    heartbeat=1.0,
                    should_stop=lambda: self._stop_requested,
                    include_bootstrap=True,
                    ack_capability=ALERT_ACK_PROTOCOL,
                    connection_id=self.connection_id,
                    client_id=self.client_id,
                ):
                    if self._stop_requested:
                        break
                    if not isinstance(event, dict):
                        continue
                    if not connection_announced:
                        self.status_changed.emit("connected", "")
                        connection_announced = True
                    self._last_success_at = heartbeat_now_iso()
                    event_name = str(event.get("event") or "").strip()
                    data = event.get("data")
                    if event_name == "bootstrap" and isinstance(data, dict):
                        self._drain_alert_acks(api)
                        self.bootstrap_received.emit(data)
                        self._post_heartbeat(api, "connected")
                        remember_cursor(event.get("id", ""))
                        continue
                    if event_name == "safe" and isinstance(data, dict):
                        self._drain_alert_acks(api)
                        self.safe_received.emit(data)
                        self._post_heartbeat(api, "safe:1")
                        remember_cursor(event.get("id", ""))
                        continue
                    if event_name != "alert" or not isinstance(data, dict):
                        self._drain_alert_acks(api)
                        self._post_heartbeat(api, "connected")
                        remember_cursor(event.get("id", ""))
                        continue
                    alert = data
                    if self.consumer.accept(alert):
                        self.alert_received.emit(alert)
                        self._post_heartbeat(api, "alert:1")
                    else:
                        self._post_heartbeat(api, "connected")
                    self._drain_alert_acks(api)
                    remember_cursor(event.get("id", ""))
                backoff = 1.0
            except IntelApiError as exc:
                retrying_after_error = True
                message = summarize_heartbeat_error(str(exc))
                self.status_changed.emit("error", message)
                self._post_heartbeat(api, "error", message, force=True)
                self._sleep_with_stop(backoff)
                backoff = min(self.reconnect_max_delay, backoff * 2)
            except Exception as exc:
                retrying_after_error = True
                message = summarize_heartbeat_error(str(exc))
                self.status_changed.emit("error", message)
                self._post_heartbeat(api, "error", message, force=True)
                self._sleep_with_stop(backoff)
                backoff = min(self.reconnect_max_delay, backoff * 2)
            if retrying_after_error and not self._stop_requested:
                self.status_changed.emit("reconnecting", "")
                self._post_heartbeat(api, "reconnecting")
        close = getattr(api, "close", None)
        if callable(close):
            close()

    def _drain_alert_acks(self, api: IntelApiClient) -> None:
        """Send queued UI confirmations without turning them into a charge."""
        while True:
            try:
                alert = self._ack_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._post_alert_ack(api, alert)
            except IntelApiError as exc:
                # Keep the evidence for the next heartbeat/reconnect.  The
                # server-side Idempotency-Key makes every retry harmless.
                logger.warning("Alert acknowledgement failed: %s", exc)
                self._ack_queue.put(alert)
                return

    def _post_alert_ack(self, api: IntelApiClient, alert: dict[str, Any]) -> None:
        """ACK only a delivery carrying all server binding evidence."""
        delivery = alert.get("delivery")
        if not isinstance(delivery, dict):
            delivery = alert.get("billing")
        if not isinstance(delivery, dict) and alert.get("delivery_id"):
            delivery = alert
        if not isinstance(delivery, dict):
            # Ordinary free alerts have no Seat delivery and must not be
            # guessed into a chargeable interval.
            return
        required = (
            "delivery_id",
            "charge_event_id",
            "revision",
            "connection_id",
            "started_at",
            "ended_at",
            "duration_seconds",
        )
        if any(str(delivery.get(field) or "").strip() == "" for field in required):
            logger.warning("Ignoring alert delivery without complete binding evidence")
            return
        connection_id = str(delivery["connection_id"]).strip()
        delivery_id = str(delivery["delivery_id"]).strip()
        ack_key = f"alert-ack:{delivery_id}:{int(delivery['revision'])}"
        evidence = {
            "schema_version": "alert-use-evidence.v1",
            "alert_id": str(alert.get("id") or "").strip(),
            "event_id": str(alert.get("event_id") or alert.get("id") or "").strip(),
            "client_id": self.client_id,
            "client_version": self._runtime["client_version"],
            "host": self._runtime["host"],
            "connection_id": connection_id,
            "received_at": heartbeat_now_iso(),
            "ui_handled_at": heartbeat_now_iso(),
            "dedupe": "accepted",
            "ui_delivery": "alert_received",
        }
        source_evidence = alert.get("evidence")
        if isinstance(source_evidence, (dict, list)):
            evidence["source_evidence"] = source_evidence
        api.ack_alert_delivery(
            delivery_id,
            charge_event_id=str(delivery["charge_event_id"]),
            revision=int(delivery["revision"]),
            connection_id=connection_id,
            started_at=str(delivery["started_at"]),
            ended_at=str(delivery["ended_at"]),
            duration_seconds=int(delivery["duration_seconds"]),
            ack_idempotency_key=ack_key,
            evidence=evidence,
        )

    def _sleep_with_stop(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.2, deadline - time.monotonic()))

    def _post_heartbeat(
        self,
        api: IntelApiClient,
        action: str,
        error: str = "",
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        alert_scope = self._alert_scope_snapshot()
        scope_changed = alert_scope["revision"] != self._last_heartbeat_scope_revision
        if not force and not scope_changed and now < self._last_heartbeat_at + self.heartbeat_interval:
            return
        try:
            api.post_heartbeat(
                client_id=self.client_id,
                client_type=ALERT_CLIENT_TYPE,
                label=ALERT_CLIENT_LABEL,
                heartbeat_interval_seconds=self.heartbeat_interval,
                details=build_heartbeat_details(
                    last_action=action,
                    last_error=error,
                    client_version=self._runtime["client_version"],
                    host=self._runtime["host"],
                    last_success_at=self._last_success_at,
                    connection_id=self.connection_id,
                    alert_scope=alert_scope,
                ),
            )
            self._last_heartbeat_at = now
            # Record the sent snapshot only. A selection made during HTTP I/O
            # remains pending and bypasses throttling on the next SSE keepalive.
            self._last_heartbeat_scope_revision = alert_scope["revision"]
        except IntelApiError as exc:
            logger.warning("Heartbeat update failed: %s", exc)


class AlertMapWorker(QThread):
    """Fetch one bounded map neighborhood without blocking the overlay UI."""

    map_received = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        server: str,
        *,
        system_names: list[str],
        system_ids: list[int],
        hops: int,
        api_key: str = "",
        api_factory: Callable[..., IntelApiClient] = IntelApiClient,
    ) -> None:
        super().__init__()
        self.server = server
        self.system_names = list(system_names)
        self.system_ids = list(system_ids)
        self.hops = max(0, min(MAX_MAP_NEIGHBORHOOD_HOPS, int(hops)))
        self.api_key = str(api_key or "").strip()
        self.api_factory = api_factory

    def run(self) -> None:
        api = self.api_factory(self.server, timeout=5.0, api_key=self.api_key)
        try:
            payload = api.map_neighborhood(
                system_names=self.system_names,
                system_ids=self.system_ids,
                hops=self.hops,
            )
            self.map_received.emit(payload)
        except Exception as exc:
            self.failed.emit(summarize_heartbeat_error(str(exc)))
        finally:
            close = getattr(api, "close", None)
            if callable(close):
                close()


class AlertTrayController:
    """Wire the overlay, tray menu, sound, and SSE worker together."""

    def __init__(
        self,
        app: QApplication,
        args: argparse.Namespace,
        *,
        api_factory: Callable[..., IntelApiClient] = IntelApiClient,
        tray_enabled: bool = True,
        notification_callback: Callable[[str, str], None] | None = None,
        window_provider: Callable[[], list[dict[str, Any]]] | None = None,
        system_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.app = app
        self.args = args
        self.state = AlertClientState(args.state)
        self.state.load_seen_ids()
        self.api_factory = api_factory
        self._tray_enabled = bool(tray_enabled)
        self._notification_callback = notification_callback
        self.overlay = AlertOverlay()
        self.overlay.set_map_selection_memory(self.state.map_selected_account_keys())
        self.overlay.set_alert_range_hops(self.state.alert_range_hops())
        self.overlay.map_options_changed.connect(self._on_map_options_changed)
        self.overlay.sound_stop_requested.connect(self._stop_current_alert_sound)
        self.overlay.geometry_changed.connect(self.state.save_overlay_geometry)
        self.overlay.set_status("连接中", "warn")
        self.overlay.show()
        if not self.overlay.restore_geometry_state(self.state.overlay_geometry()):
            self.overlay.move_to_default_position()
        self._recent_summaries: list[dict[str, Any]] = []
        self._local_hostile_counts: dict[str, tuple[str, int]] = {}
        self._map_accounts: list[dict[str, Any]] = []
        self._local_map_accounts: list[dict[str, Any]] = []
        self._remote_map_accounts: list[dict[str, Any]] = []
        self._local_system_cache: dict[str, tuple[float, str]] = {}
        self._local_window_capturer: Capturer | None = None
        if window_provider is None:
            self._local_window_capturer = Capturer()
            window_provider = lambda: self._local_window_capturer.list_eve_windows(
                "EVE -"
            )
        self._window_provider = window_provider
        self._location_cache_seconds = 0.0 if system_resolver is not None else 5.0
        self._system_resolver = system_resolver or (
            lambda character_name: find_latest_local_system(
                DEFAULT_CHATLOG_DIR,
                character_name=character_name,
            )
        )
        self._map_worker: AlertMapWorker | None = None
        self._active_map_request_signature: tuple[Any, ...] | None = None
        self._pending_map_request: dict[str, Any] | None = None
        self._map_request_signature: tuple[Any, ...] | None = None
        self._alert_range_systems: set[str] = set()
        self._confirmed_alert_range: set[str] | None = None
        self._alert_range_pending = False
        self._range_live_announced: set[str] = set()
        self._deferred_alerts: dict[str, dict[str, Any]] = {}
        self._stopped = False
        self._alert_volume = max(0.0, min(1.0, float(getattr(args, "alert_volume", 1.0))))
        self._alert_muted = bool(getattr(args, "alert_muted", False))
        self._alert_sound_mode = str(
            getattr(args, "alert_sound_mode", "interval") or "interval"
        ).casefold()
        if self._alert_sound_mode not in {"interval", "continuous"}:
            self._alert_sound_mode = "interval"
        self._alert_sound_path = str(
            getattr(args, "alert_sound_path", "") or ""
        ).strip()
        self._alert_cooldown = max(0.0, float(getattr(args, "alert_cooldown", 15.0)))
        repeat_interval = max(
            1.0,
            min(60.0, float(getattr(args, "alert_repeat_interval", 2.0))),
        )
        self._alert_repeat_interval_ms = int(repeat_interval * 1000)
        self._alert_repeat_count = max(
            1,
            min(10, int(getattr(args, "alert_repeat_count", 3))),
        )
        self._remaining_sound_plays = 0
        self._repeating_alert_system: str | None = None
        self._continuous_sound: QSoundEffect | None = None
        self._active_alert_systems: set[str] = set()
        self._continuous_sound_snoozed = False
        self._sound_repeat_timer = QTimer(self.overlay)
        self._sound_repeat_timer.setSingleShot(True)
        self._sound_repeat_timer.timeout.connect(self._play_next_alert_sound)
        self._last_notified: dict[str, tuple[float, int]] = {}
        self._tray: QSystemTrayIcon | None = None
        self._worker = AlertEventWorker(
            args.server,
            self.state,
            timeout=args.timeout,
            heartbeat_interval=args.heartbeat_interval,
            reconnect_max_delay=args.reconnect_max_delay,
            api_key=str(getattr(args, "api_key", "") or ""),
            api_factory=api_factory,
        )
        if self._tray_enabled:
            self._setup_tray()
        self._worker.alert_received.connect(self._on_alert)
        self._worker.safe_received.connect(self._on_safe)
        self._worker.bootstrap_received.connect(self._on_bootstrap)
        self._worker.status_changed.connect(self._on_status)
        self._local_account_timer = QTimer(self.overlay)
        self._local_account_timer.setInterval(3000)
        self._local_account_timer.timeout.connect(self._refresh_local_accounts)
        self._local_account_timer.start()
        self._refresh_local_accounts()

    def start(self) -> None:
        """Start the tray icon and SSE worker."""
        if self._tray is not None:
            self._tray.show()
        self._worker.start()

    def update_alert_preferences(self, preferences: dict[str, Any]) -> None:
        """Apply sound preferences immediately while the alert client is running."""
        previous_path = str(getattr(self, "_alert_sound_path", "") or "")
        self._alert_muted = bool(preferences.get("muted", False))
        self._alert_volume = max(
            0.0,
            min(1.0, float(preferences.get("volume", 1.0))),
        )
        mode = str(
            preferences.get("sound_mode", "interval") or "interval"
        ).casefold()
        self._alert_sound_mode = (
            mode if mode in {"interval", "continuous"} else "interval"
        )
        self._alert_sound_path = str(
            preferences.get("sound_path", "") or ""
        ).strip()
        self._alert_repeat_interval_ms = int(
            max(1.0, min(60.0, float(preferences.get("repeat_interval", 2.0))))
            * 1000
        )
        self._alert_repeat_count = max(
            1,
            min(10, int(preferences.get("repeat_count", 3))),
        )

        sound = getattr(self, "_continuous_sound", None)
        if sound is not None:
            sound.setVolume(self._alert_volume)
        active_systems = getattr(self, "_active_alert_systems", set())
        if (
            self._alert_muted
            or self._alert_sound_mode != "continuous"
            or not active_systems
        ):
            self._stop_continuous_alert_sound()
            return
        if sound is not None and previous_path != self._alert_sound_path:
            self._stop_continuous_alert_sound()
        self._start_continuous_alert_sound()

    def stop(self, *, wait_for_worker: bool = True) -> None:
        """Request shutdown, optionally waiting for the SSE worker to exit."""
        self._stopped = True
        self._pending_map_request = None
        self._map_request_signature = None
        local_account_timer = getattr(self, "_local_account_timer", None)
        if local_account_timer is not None:
            local_account_timer.stop()
        sound_timer = getattr(self, "_sound_repeat_timer", None)
        if sound_timer is not None:
            sound_timer.stop()
        self._remaining_sound_plays = 0
        self._stop_continuous_alert_sound()
        state = getattr(self, "state", None)
        geometry_state = getattr(self.overlay, "geometry_state", None)
        if state is not None and callable(geometry_state):
            state.save_overlay_geometry(geometry_state())
        self._worker.stop()
        map_worker = getattr(self, "_map_worker", None)
        if map_worker is not None and map_worker.isRunning() and wait_for_worker:
            map_worker.wait(6000)
        self.overlay.hide()
        if self._tray is not None:
            self._tray.hide()
        if wait_for_worker and self._worker.isRunning():
            self._worker.wait(int((self.args.timeout + 4.0) * 1000))
        local_capturer = getattr(self, "_local_window_capturer", None)
        if local_capturer is not None:
            local_capturer.close()

    def _refresh_local_accounts(self, *, refresh_location: bool = False) -> None:
        """Refresh local account choices independently from monitor workers."""
        provider = getattr(self, "_window_provider", None)
        if not callable(provider):
            return
        try:
            windows = provider()
        except Exception as exc:
            logger.debug("Local EVE window discovery unavailable: %s", exc)
            return
        if not isinstance(windows, list):
            windows = []

        now = time.monotonic()
        resolver = getattr(self, "_system_resolver", None)
        cache = getattr(self, "_local_system_cache", {})
        systems: dict[str, str] = {}
        for window in windows:
            title = str(window.get("title") or "").strip()
            prefix, separator, character_name = title.partition(" - ")
            if not separator or prefix.strip().casefold() != "eve":
                character_name = str(window.get("character_name") or "").strip()
            else:
                character_name = character_name.strip()
            character_key = character_name.casefold()
            if not character_key:
                continue
            expires_at, cached_system = cache.get(character_key, (0.0, "Unknown"))
            if (refresh_location or now >= expires_at) and callable(resolver):
                try:
                    resolved = resolver(character_name)
                    cached_system = str(
                        getattr(resolved, "system_name", resolved) or "Unknown"
                    ).strip() or "Unknown"
                except Exception as exc:
                    logger.debug("Local system lookup unavailable for %s: %s", character_name, exc)
                cache[character_key] = (now + self._location_cache_seconds, cached_system)
            systems[character_key] = cached_system
        self._local_system_cache = {
            key: value for key, value in cache.items() if key in systems
        }
        self._local_map_accounts = local_accounts_from_windows(windows, systems)
        self._sync_map_accounts()

    def _sync_map_accounts(self) -> None:
        """Apply the merged local and remote account list to the overlay."""
        accounts = merge_map_accounts(
            getattr(self, "_local_map_accounts", []),
            getattr(self, "_remote_map_accounts", []),
        )
        previous_signature = tuple(sorted(
            (
                str(item.get("key") or ""),
                str(item.get("system_name") or ""),
                str(item.get("system_id") or ""),
                _account_is_monitoring(item),
                _account_health_status(item),
            )
            for item in getattr(self, "_map_accounts", [])
        ))
        current_signature = tuple(sorted(
            (
                str(item.get("key") or ""),
                str(item.get("system_name") or ""),
                str(item.get("system_id") or ""),
                _account_is_monitoring(item),
                _account_health_status(item),
            )
            for item in accounts
        ))
        changed = current_signature != previous_signature
        self._map_accounts = accounts
        if changed:
            set_accounts = getattr(self.overlay, "set_map_accounts", None)
            if callable(set_accounts):
                set_accounts(accounts)
        if changed or getattr(self, "_map_request_signature", None) is None:
            self._refresh_local_map()

    def is_running(self) -> bool:
        """Return whether any controller worker is still unwinding."""
        map_worker = getattr(self, "_map_worker", None)
        return self._worker.isRunning() or bool(
            map_worker is not None and map_worker.isRunning()
        )

    def show_monitoring_systems(self, system_names: list[str]) -> None:
        """Ensure monitored systems are visible before the first SSE refresh."""
        self._refresh_local_accounts(refresh_location=True)
        known_systems = {
            str(item.get("system_name") or "Unknown").casefold()
            for item in self._recent_summaries
        }
        for value in system_names:
            system = str(value or "").strip()
            key = system.casefold()
            if not system or key == "unknown" or key in known_systems:
                continue
            known_systems.add(key)
            self._recent_summaries.append(
                {
                    "system_name": system,
                    "hostile_count": 0,
                    "active_hostile_count": 0,
                    "created_at": "",
                    "active": False,
                }
            )
        self._sync_alert_display()

    def forget_local_monitoring_systems(self, system_names: list[str]) -> None:
        """Remove local node state without emitting a hostile-safe notification."""
        system_keys = {
            str(system or "").strip().casefold()
            for system in system_names
            if str(system or "").strip()
        }
        if not system_keys:
            return
        local_counts = getattr(self, "_local_hostile_counts", {})
        locally_tracked = system_keys.intersection(local_counts)
        for key in system_keys:
            local_counts.pop(key, None)
        self._recent_summaries = [
            item
            for item in self._recent_summaries
            if str(item.get("system_name") or "Unknown").casefold()
            not in locally_tracked
            or not item.get("active", True)
        ]
        self._sync_alert_display()

    def set_anchor_window(self, window: dict[str, Any] | None) -> None:
        """Place the embedded overlay on the EVE window's display."""
        self.overlay.set_anchor_rect(window)

    def update_local_hostile_count(self, system_name: str, count: int) -> None:
        """Apply authoritative red-icon evidence from this monitor process."""
        if getattr(self, "_server_authority", False) and allows_remote(getattr(self, "_monitoring_scope", None), system_name):
            return
        system = str(system_name or "Unknown").strip() or "Unknown"
        key = system.casefold()
        hostile_count = max(0, int(count))
        local_counts = getattr(self, "_local_hostile_counts", {})
        self._local_hostile_counts = local_counts
        if hostile_count > 0:
            local_counts[key] = (system, hostile_count)
            self._on_alert(
                {
                    "system_name": system,
                    "hostile_count": hostile_count,
                    "_local_visual": True,
                }
            )
            return
        local_counts.pop(key, None)
        self._on_safe(
            {
                "system_name": system,
                "hostile_count": 0,
                "message": f"✅ {system} 清空",
                "_local_visual": True,
            }
        )

    def _apply_local_hostile_counts(self) -> None:
        """Keep local visual counts from being reduced by delayed server state."""
        for system, hostile_count in getattr(
            self, "_local_hostile_counts", {}
        ).values():
            if getattr(self, "_server_authority", False) and allows_remote(getattr(self, "_monitoring_scope", None), system):
                continue
            existing = next(
                (
                    item
                    for item in self._recent_summaries
                    if str(item.get("system_name") or "Unknown").casefold()
                    == system.casefold()
                ),
                None,
            )
            if existing is None:
                existing = {
                    "system_name": system,
                    "created_at": "",
                }
                self._recent_summaries.append(existing)
            existing.update(
                {
                    "hostile_count": hostile_count,
                    "active_hostile_count": hostile_count,
                    "active": True,
                }
            )

    def _on_map_options_changed(self, selected_keys: list[str], hops: int) -> None:
        memory = self.overlay.map_selection_memory()
        if memory is not None:
            self.state.save_map_selected_account_keys(memory)
        self.state.save_alert_range_hops(hops)
        self._refresh_local_map(selected_keys=selected_keys, hops=hops)

    def _refresh_local_map(
        self,
        *,
        selected_keys: list[str] | None = None,
        hops: int | None = None,
    ) -> None:
        if selected_keys is None or hops is None:
            selection = getattr(self.overlay, "map_selection", None)
            selected_keys, hops = selection() if callable(selection) else ([], DEFAULT_ALERT_RANGE_HOPS)
        selected_set = {str(item) for item in selected_keys or [] if str(item)}
        accounts = [
            item
            for item in getattr(self, "_map_accounts", [])
            if str(item.get("key") or "") in selected_set
        ]
        set_scope = getattr(getattr(self, "_worker", None), "set_alert_scope", None)
        if callable(set_scope):
            # Local chatlogs know names without IDs. The server resolves these
            # exact centers; map neighbors and monitoring rewards are unrelated.
            set_scope([str(item.get("system_name") or "") for item in accounts])
        if not accounts:
            self._pending_map_request = None
            self._map_request_signature = None
            self._set_alert_range(set(), confirmed=True)
            setter = getattr(self.overlay, "set_map_payload", None)
            if callable(setter):
                setter({"systems": [], "links": [], "centers": []})
            message = getattr(self.overlay, "set_map_message", None)
            if callable(message):
                message("请选择至少一个运行中的账号")
            return

        system_names = list(
            dict.fromkeys(
                str(item.get("system_name") or "").strip()
                for item in accounts
                if str(item.get("system_name") or "").strip()
                and str(item.get("system_name") or "").strip().casefold()
                != "unknown"
            )
        )
        system_ids: list[int] = []
        for item in accounts:
            try:
                system_id = int(item.get("system_id") or 0)
            except (TypeError, ValueError):
                continue
            if system_id > 0 and system_id not in system_ids:
                system_ids.append(system_id)
        if not system_names and not system_ids:
            self._pending_map_request = None
            self._map_request_signature = None
            self._set_alert_range(set(), confirmed=True)
            setter = getattr(self.overlay, "set_map_payload", None)
            if callable(setter):
                setter({"systems": [], "links": [], "centers": []})
            message = getattr(self.overlay, "set_map_message", None)
            if callable(message):
                message("所选账号尚未识别当前星系")
            return
        request = {
            "system_names": system_names,
            "system_ids": system_ids,
            "hops": max(0, min(MAX_MAP_NEIGHBORHOOD_HOPS, int(hops))),
        }
        request["system_names"].sort(key=str.casefold)
        request["system_ids"].sort()
        signature = (tuple(request["system_names"]), tuple(request["system_ids"]), request["hops"])
        if signature == getattr(self, "_map_request_signature", None):
            return
        self._map_request_signature = signature
        self._set_alert_range({name.casefold() for name in system_names})
        setter = getattr(self.overlay, "set_map_payload", None)
        if callable(setter):
            setter({"systems": [], "links": [], "centers": []})
        message = getattr(self.overlay, "set_map_message", None)
        if callable(message):
            message("正在更新预警范围…")
        worker = getattr(self, "_map_worker", None)
        if worker is not None:
            self._pending_map_request = request
            return
        self._start_map_worker(request)

    def _start_map_worker(self, request: dict[str, Any]) -> None:
        if bool(getattr(self, "_stopped", False)):
            return
        worker = AlertMapWorker(
            self.args.server,
            system_names=request["system_names"],
            system_ids=request["system_ids"],
            hops=request["hops"],
            api_key=str(getattr(self.args, "api_key", "") or ""),
            api_factory=self.api_factory,
        )
        self._map_worker = worker
        self._active_map_request_signature = (
            tuple(request["system_names"]),
            tuple(request["system_ids"]),
            request["hops"],
        )
        worker.map_received.connect(self._on_map_received)
        worker.failed.connect(self._on_map_failed)
        worker.finished.connect(self._on_map_worker_finished)
        worker.start()

    def _on_map_received(self, payload: dict[str, Any]) -> None:
        active_request = self._active_map_request_signature
        if (
            self._stopped
            or active_request is None
            or active_request != self._map_request_signature
        ):
            return
        self._pending_map_request = None
        setter = getattr(self.overlay, "set_map_payload", None)
        if callable(setter):
            setter(payload)
        systems = {
            str(item.get("name") or "").strip().casefold()
            for item in payload.get("systems", [])
            if isinstance(item, dict) and item.get("name")
        }
        systems.update(name.casefold() for name in active_request[0])
        self._set_alert_range(systems, confirmed=True)

    def _on_map_failed(self, message: str) -> None:
        if bool(getattr(self, "_stopped", False)) or getattr(
            self, "_active_map_request_signature", None
        ) != getattr(self, "_map_request_signature", None):
            return
        setter = getattr(self.overlay, "set_map_message", None)
        if callable(setter):
            setter("预警范围加载失败，正在重试")
        logger.warning("Alert range load failed: %s", message)
        self._map_request_signature = None

    def _on_map_worker_finished(self) -> None:
        self._map_worker = None
        self._active_map_request_signature = None
        pending = self._pending_map_request
        self._pending_map_request = None
        if pending is not None and not bool(getattr(self, "_stopped", False)):
            self._start_map_worker(pending)

    def _setup_tray(self) -> None:
        icon = self.overlay.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        self.overlay.setWindowIcon(icon)
        tray = QSystemTrayIcon(self.overlay)
        tray.setIcon(icon)
        tray.setToolTip("EVE Sentry Alert")
        tray.activated.connect(self._on_tray_activated)

        menu = QMenu()
        toggle_action = QAction("Show / Hide Overlay")
        toggle_action.triggered.connect(self._toggle_overlay)
        menu.addAction(toggle_action)

        reconnect_action = QAction("Reconnect")
        reconnect_action.triggered.connect(self._restart_worker)
        menu.addAction(reconnect_action)

        menu.addSeparator()

        quit_action = QAction("Quit")
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        self._tray = tray

    def _notify(self, title: str, message: str) -> None:
        """Deliver a notification through the host or standalone tray."""
        if self._notification_callback is not None:
            self._notification_callback(title, message)
            return
        if self._tray is not None:
            self._tray.showMessage(title, message)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_overlay()

    def _toggle_overlay(self) -> None:
        if self.overlay.isVisible():
            self.overlay.hide()
            return
        self.overlay.show()
        self.overlay.raise_()

    def _restart_worker(self) -> None:
        if bool(getattr(self, "_worker_restart_pending", False)):
            return
        self._worker_restart_pending = True
        set_status = getattr(getattr(self, "overlay", None), "set_status", None)
        if callable(set_status):
            set_status("重连中", "warn")
        worker = self._worker
        if worker.isRunning():
            finished = getattr(worker, "finished", None)
            connect = getattr(finished, "connect", None)
            if callable(connect):
                connect(
                    lambda worker=worker: self._complete_worker_restart(worker)
                )
                worker.stop()
                return
            worker.stop()
        self._complete_worker_restart(worker)

    def _complete_worker_restart(self, previous_worker: AlertEventWorker) -> None:
        """Start a replacement SSE worker after the previous one has exited."""
        if self._worker is not previous_worker:
            return
        if bool(getattr(self, "_stopped", False)):
            self._worker_restart_pending = False
            return
        self._worker = AlertEventWorker(
            self.args.server,
            self.state,
            timeout=self.args.timeout,
            heartbeat_interval=self.args.heartbeat_interval,
            reconnect_max_delay=self.args.reconnect_max_delay,
            api_key=str(getattr(self.args, "api_key", "") or ""),
            api_factory=self.api_factory,
        )
        self._worker.alert_received.connect(self._on_alert)
        self._worker.safe_received.connect(self._on_safe)
        self._worker.bootstrap_received.connect(self._on_bootstrap)
        self._worker.status_changed.connect(self._on_status)
        if callable(getattr(getattr(self, "overlay", None), "map_selection", None)):
            self._refresh_local_map()
        self._worker.start()
        self._worker_restart_pending = False

    def _on_status(self, status: str, message: str) -> None:
        if status in {"reconnecting", "error"}:
            self._discard_realtime_state()
        self._connection_display = {
            "connected": ("SSE 在线", "ok"),
            "reconnecting": ("重连中", "warn"),
            "error": ("连接异常", "danger"),
        }.get(status, (status, "idle"))
        self.overlay.set_status(*self._connection_display)
        if status == "error" and message:
            self._notify("EVE Sentry Alert", message)

    def _remote_alerts_allowed(self) -> bool:
        """A detector whose windows are all outside the map only warns locally."""
        scope = getattr(self, "_monitoring_scope", None)
        if not isinstance(scope, dict) or not scope.get("enabled"):
            return True
        provider = getattr(self, "_monitoring_system_provider", None)
        systems = list(provider()) if callable(provider) else [
            item.get("system_name") for item in getattr(self, "_local_map_accounts", [])
        ]
        return not systems or any(allows_remote(scope, name) for name in systems)

    def _system_in_alert_range(self, system_key: str) -> bool:
        systems = getattr(self, "_alert_range_systems", None)
        return systems is None or system_key in systems

    def _sync_alert_display(self) -> list[dict[str, Any]]:
        scoped = [
            item for item in self._recent_summaries
            if self._system_in_alert_range(alert_system_name(item).casefold())
        ]
        active = [
            item for item in scoped
            if item.get("active", True) and int(item.get("hostile_count") or 0) > 0
        ]
        self._active_alert_systems = {alert_system_name(item).casefold() for item in active}
        repeating_system = getattr(self, "_repeating_alert_system", None)
        if not active or (repeating_system and repeating_system not in self._active_alert_systems):
            self._remaining_sound_plays = 0
            self._repeating_alert_system = None
            timer = getattr(self, "_sound_repeat_timer", None)
            if timer is not None:
                timer.stop()
        if not active:
            self._stop_continuous_alert_sound()
            if not getattr(self, "_alert_range_pending", False):
                self._continuous_sound_snoozed = False
        self.overlay.show_summaries(scoped)
        return active

    def _set_alert_range(self, systems: set[str], *, confirmed: bool = False) -> None:
        self._alert_range_systems = systems
        self._alert_range_pending = not confirmed
        active = self._sync_alert_display()
        if not active and hasattr(self, "_connection_display"):
            self.overlay.set_status(*self._connection_display)
        if not confirmed:
            return
        previous = self._confirmed_alert_range
        self._confirmed_alert_range = systems
        deferred, self._deferred_alerts = self._deferred_alerts, {}
        announced, self._range_live_announced = self._range_live_announced, set()
        if not self._active_alert_systems.intersection((previous or set()) | announced):
            self._continuous_sound_snoozed = False
        for summary in active:
            key = alert_system_name(summary).casefold()
            alert = deferred.get(key)
            if key not in announced and (
                alert is not None or (previous is not None and key not in previous)
            ):
                self._announce_alert(summary, alert)
        if (
            active
            and not self._alert_muted
            and self._alert_sound_mode == "continuous"
            and not self._continuous_sound_snoozed
            and self._active_alert_systems.intersection(self._last_notified)
        ):
            self._start_continuous_alert_sound()

    def _on_alert(self, alert: dict[str, Any]) -> None:
        if not alert.get("_local_visual") and not self._remote_alerts_allowed():
            return
        summary = summarize_alert(alert)
        summary["active_hostile_count"] = summary["hostile_count"]
        system = str(summary.get("system_name") or "Unknown")
        system_key = system.casefold()
        if summary.get("freshness") == "unknown":
            self._discard_realtime_state(system_key)
            if self._system_in_alert_range(system_key):
                self.overlay.set_status("采集异常", "warn")
            return  # Removing unavailable data must never announce safety or arrival.
        existing = next(
            (
                item
                for item in self._recent_summaries
                if str(item.get("system_name") or "Unknown").casefold()
                == system_key
            ),
            None,
        )
        if existing is None:
            self._recent_summaries.append(summary)
        else:
            existing.pop("freshness", None)
            existing.update(summary)
        self._apply_local_hostile_counts()
        if not self._system_in_alert_range(system_key):
            if self._alert_range_pending:
                self._deferred_alerts[system_key] = alert
            return
        if not getattr(self, "_active_alert_systems", set()):
            self._continuous_sound_snoozed = False
        self._sync_alert_display()
        if not summary["active"]:
            return
        if getattr(self, "_alert_range_pending", False):
            self._range_live_announced.add(system_key)
            self._deferred_alerts.pop(system_key, None)
        self._announce_alert(summary, alert)

    def _announce_alert(
        self, summary: dict[str, Any], alert: dict[str, Any] | None = None
    ) -> None:
        self.overlay.set_status("新告警", "danger")
        queue_ack = getattr(getattr(self, "_worker", None), "queue_alert_ack", None)
        if alert is not None and callable(queue_ack):
            queue_ack(alert)
        hostile_count = int(summary.get("hostile_count") or 0)
        notification_key = alert_system_name(summary).casefold()
        last_notified = getattr(self, "_last_notified", {})
        self._last_notified = last_notified
        previous_notification = last_notified.get(notification_key)
        now = time.monotonic()
        in_cooldown = bool(
            previous_notification
            and now - previous_notification[0]
            < float(getattr(self, "_alert_cooldown", 0.0))
        )
        muted = bool(getattr(self, "_alert_muted", False))
        if not muted:
            if getattr(self, "_alert_sound_mode", "interval") == "continuous":
                if not self._continuous_sound_snoozed:
                    self._start_continuous_alert_sound()
            elif not in_cooldown:
                self._repeating_alert_system = notification_key
                self._play_alert_sound_sequence()
        if in_cooldown:
            return
        last_notified[notification_key] = (now, hostile_count)
        self._notify(
            "敌对告警",
            f"❗ {summary['system_name']} 来敌 {hostile_count} 人",
        )

    def _discard_realtime_state(self, system_key: str | None = None) -> None:
        """Remove unavailable evidence and its sounds without manufacturing a clear."""
        def keep(item):
            return system_key is not None and str(item.get("system_name") or "").casefold() != system_key

        self._recent_summaries = [item for item in self._recent_summaries if keep(item)]
        self._remote_map_accounts = [item for item in getattr(self, "_remote_map_accounts", []) if keep(item)]
        counts = getattr(self, "_local_hostile_counts", {})
        self._local_hostile_counts = {key: value for key, value in counts.items() if system_key is not None and key != system_key}
        deferred = getattr(self, "_deferred_alerts", {})
        if system_key is None:
            deferred.clear()
            getattr(self, "_range_live_announced", set()).clear()
        else:
            deferred.pop(system_key, None)
            getattr(self, "_range_live_announced", set()).discard(system_key)
        self._sync_alert_display()
        self._sync_map_accounts()

    def _play_alert_sound_sequence(self) -> None:
        """Play the configured number of alert sounds without blocking the UI."""
        timer = getattr(self, "_sound_repeat_timer", None)
        if timer is not None:
            timer.stop()
        self._remaining_sound_plays = max(
            1,
            int(getattr(self, "_alert_repeat_count", 1)),
        )
        self._play_next_alert_sound()

    def _play_next_alert_sound(self) -> None:
        if bool(getattr(self, "_alert_muted", False)):
            self._remaining_sound_plays = 0
            return
        remaining = int(getattr(self, "_remaining_sound_plays", 0))
        if remaining <= 0:
            return
        volume = getattr(self, "_alert_volume", None)
        sound_path = str(getattr(self, "_alert_sound_path", "") or "").strip()
        if volume is None and not sound_path:
            play_alert_sound()
        elif volume is None:
            play_alert_sound(sound_path=sound_path)
        elif sound_path:
            play_alert_sound(volume, sound_path)
        else:
            play_alert_sound(volume)
        remaining -= 1
        self._remaining_sound_plays = remaining
        timer = getattr(self, "_sound_repeat_timer", None)
        if remaining > 0 and timer is not None:
            timer.start(int(getattr(self, "_alert_repeat_interval_ms", 2000)))

    def _start_continuous_alert_sound(self) -> None:
        if bool(getattr(self, "_continuous_sound_snoozed", False)):
            return
        sound = getattr(self, "_continuous_sound", None)
        if sound is not None:
            return
        bundled_path = Path(__file__).parent.parent / "resources" / "alert.wav"
        configured_path = str(getattr(self, "_alert_sound_path", "") or "").strip()
        candidate = Path(configured_path).expanduser() if configured_path else None
        sound_path = (
            candidate
            if candidate is not None
            and candidate.is_file()
            and candidate.suffix.casefold() == ".wav"
            else bundled_path
        )
        if not sound_path.is_file():
            return
        try:
            sound = QSoundEffect()
            sound.setSource(QUrl.fromLocalFile(str(sound_path.resolve())))
            sound.setVolume(max(0.0, min(1.0, float(getattr(self, "_alert_volume", 1.0)))))
            sound.setLoopCount(-2)
            sound.play()
            self._continuous_sound = sound
            self._set_sound_stop_available(True)
        except Exception as exc:
            logger.warning("Failed to start continuous alert sound: %s", exc)

    def _stop_current_alert_sound(self) -> None:
        """Stop and snooze the current continuous alert wave until it is safe."""
        self._continuous_sound_snoozed = True
        self._stop_continuous_alert_sound()

    def _set_sound_stop_available(self, available: bool) -> None:
        """Reflect continuous sound availability in the overlay when present."""
        overlay = getattr(self, "overlay", None)
        setter = getattr(overlay, "set_sound_stop_available", None)
        if callable(setter):
            setter(bool(available))

    def _stop_continuous_alert_sound(self) -> None:
        sound = getattr(self, "_continuous_sound", None)
        self._continuous_sound = None
        self._set_sound_stop_available(False)
        if sound is not None:
            try:
                sound.stop()
            except Exception:
                pass

    def _on_safe(self, event: dict[str, Any]) -> None:
        """Notify once after the final hostile leaves a solar system."""
        if not event.get("_local_visual") and not self._remote_alerts_allowed():
            return
        system_name = str(
            event.get("system_name") or event.get("system") or "Unknown"
        ).strip() or "Unknown"
        system_key = system_name.casefold()
        if (not getattr(self, "_server_authority", False)
                and system_name.casefold() in getattr(self, "_local_hostile_counts", {})):
            return
        getattr(self, "_deferred_alerts", {}).pop(system_key, None)
        getattr(self, "_range_live_announced", set()).discard(system_key)
        getattr(self, "_last_notified", {}).pop(system_key, None)
        for item in self._recent_summaries:
            if str(item.get("system_name") or "Unknown").casefold() != (
                system_name.casefold()
            ):
                continue
            item["active"] = False
            item["hostile_count"] = 0
            item["active_hostile_count"] = 0
            item.pop("freshness", None)
        if not self._system_in_alert_range(system_key):
            return
        if not self._sync_alert_display():
            self.overlay.set_status("星系安全", "ok")
        message = str(event.get("message") or "").strip()
        if not message:
            message = f"✅ {system_name} 清空"
        self._notify("星系安全", message)

    def _on_bootstrap(self, bootstrap: dict[str, Any]) -> None:
        self._server_authority = bootstrap.get("state_source") == "system_current_state"
        if isinstance(bootstrap.get("monitoring_scope"), dict):
            self._monitoring_scope = bootstrap["monitoring_scope"]
            callback = getattr(self, "_monitoring_scope_callback", None)
            if callable(callback):
                callback(self._monitoring_scope)
        if not self._remote_alerts_allowed():
            bootstrap = {**bootstrap, "map": {"systems": []}, "alerts": [], "active_intel": [], "clients": {"heartbeats": []}}
        if self._server_authority:
            self._local_hostile_counts = {
                key: value for key, value in getattr(self, "_local_hostile_counts", {}).items()
                if not allows_remote(getattr(self, "_monitoring_scope", None), value[0])
            }
        self._recent_summaries = sync_alert_summaries_from_bootstrap(
            self._recent_summaries,
            bootstrap,
        )
        self._apply_local_hostile_counts()
        self._sync_alert_display()
        accounts = monitored_accounts_from_bootstrap(bootstrap)
        unknown = unavailable_systems_from_bootstrap(bootstrap)
        accounts = [item for item in accounts if item["system_name"].casefold() not in unknown]
        worker = getattr(self, "_worker", None)
        alert_client_id = str(getattr(worker, "client_id", "") or "")
        for account in accounts:
            account["local"] = is_local_monitored_account(
                account.get("client_id"),
                alert_client_id,
            )
        self._remote_map_accounts = accounts
        self._sync_map_accounts()

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--state", default=default_state_path())
    parser.add_argument("--timeout", type=float, default=DEFAULT_EVENT_TIMEOUT)
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL,
    )
    parser.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=DEFAULT_RECONNECT_MAX_DELAY,
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="start with the overlay hidden; tray menu can show it again",
    )
    return parser.parse_args(argv)


def run_alert_client(args: argparse.Namespace) -> int:
    """Run the local tray alert client."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    controller = AlertTrayController(app, args)
    if args.hidden:
        controller.overlay.hide()
    app.aboutToQuit.connect(controller.stop)
    controller.start()
    return int(app.exec())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return run_alert_client(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
