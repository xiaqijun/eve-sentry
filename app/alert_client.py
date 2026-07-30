"""Local tray alert client that consumes server-side SSE alerts only."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QEvent, QPoint, QRect, QTimer, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.core.heartbeat import (
    heartbeat_now_iso,
    monitored_system_names,
    resolve_runtime_identity,
    summarize_heartbeat_error,
)
from app.intel_client import IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)

ALERT_CLIENT_TYPE = "alert_client"
ALERT_CLIENT_LABEL = "Alert Client"
DEFAULT_EVENT_TIMEOUT = 30.0
DEFAULT_HEARTBEAT_INTERVAL = 10.0
DEFAULT_RECONNECT_MAX_DELAY = 30.0
MAX_OVERLAY_ROWS = 4
OVERLAY_TILE_COLUMNS = 2
OVERLAY_MIN_WIDTH = 140
OVERLAY_MIN_HEIGHT = 52
OVERLAY_TILE_WIDTH = 92
OVERLAY_TILE_HEIGHT = 62
OVERLAY_TILE_MIN_WIDTH = 88
OVERLAY_TILE_MAX_WIDTH = 120
OVERLAY_TILE_MIN_HEIGHT = 58
OVERLAY_TILE_MAX_HEIGHT = 74
OVERLAY_HOSTILE_COUNT_WIDTH = 38
OVERLAY_GRID_SPACING = 6
OVERLAY_RESIZE_MARGIN = 6
RESIZE_LEFT = 1
RESIZE_RIGHT = 2
RESIZE_TOP = 4
RESIZE_BOTTOM = 8


def overlay_tile_dimensions(screen_width: int, screen_height: int) -> tuple[int, int]:
    """Return compact tile dimensions for one screen's logical geometry."""
    width = int(round(max(1, screen_width) * OVERLAY_TILE_WIDTH / 1920))
    height = int(round(max(1, screen_height) * OVERLAY_TILE_HEIGHT / 1080))
    return (
        max(OVERLAY_TILE_MIN_WIDTH, min(OVERLAY_TILE_MAX_WIDTH, width)),
        max(OVERLAY_TILE_MIN_HEIGHT, min(OVERLAY_TILE_MAX_HEIGHT, height)),
    )


def default_state_path() -> str:
    """Return the default per-user alert client state path."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return str(Path(local_app_data) / "EVE Sentry" / "alert_client_state.json")
    return str(Path.home() / ".eve-sentry" / "alert_client_state.json")


class AlertClientState:
    """Persist recently received alert ids so restarts do not repeat alerts."""

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

    def load_seen_ids(self) -> list[str]:
        """Load remembered alert ids from disk."""
        self.loaded = self.path.exists()
        if not self.loaded:
            self._set_ids([])
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read alert client state from %s", self.path)
            self._set_ids([])
            return []
        if not isinstance(payload, dict):
            self._set_ids([])
            return []
        self._set_ids(self._clean_ids(payload.get("seen_alert_ids")))
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
        payload = {"version": 1, "seen_alert_ids": self._seen_ids}
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
    }


def aggregate_alert_summaries(
    summaries: list[dict[str, Any]],
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """Keep one current row per system in discovery order."""
    by_system: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in summaries:
        system = str(item.get("system_name") or "Unknown")
        active = bool(item.get("active", True))
        raw_hostile_count = item.get("hostile_count")
        try:
            hostile_count = 1 if raw_hostile_count is None else int(raw_hostile_count)
        except (TypeError, ValueError):
            hostile_count = 0
        hostile_count = max(0, hostile_count)
        active = active and hostile_count > 0
        existing = by_system.get(system)
        if existing is None:
            by_system[system] = {
                "system_name": system,
                "hostile_count": hostile_count if active else 0,
                "active_hostile_count": hostile_count if active else 0,
                "created_at": str(item.get("created_at") or ""),
                "active": active,
            }
        else:
            existing["hostile_count"] = hostile_count if active else 0
            existing["active_hostile_count"] = hostile_count if active else 0
            existing["active"] = active
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
    if not isinstance(map_systems, list):
        active_keys = active_alert_keys_from_bootstrap(bootstrap)
        updated = update_alert_summaries_active(summaries, active_keys)
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
        str(item.get("system_name") or "Unknown"): item for item in summaries
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
        existing = first_seen_by_system.get(system)
        if existing is None or first_seen < existing:
            first_seen_by_system[system] = first_seen

    current_by_system: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in map_systems:
        if not isinstance(item, dict):
            continue
        system = str(item.get("name") or item.get("system_name") or "").strip()
        try:
            hostile_count = int(item.get("hostile_count") or 0)
        except (TypeError, ValueError):
            hostile_count = 0
        if not system or hostile_count <= 0:
            continue
        previous = previous_by_system.get(system, {})
        current_by_system[system] = {
            "system_name": system,
            "hostile_count": hostile_count,
            "active_hostile_count": hostile_count,
            "created_at": first_seen_by_system.get(system)
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
        if not active_id or active_id not in hostile_active_ids or not system:
            continue
        if system in mapped_systems:
            continue
        metadata = (
            item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        )
        try:
            hostile_count = int(metadata.get("hostile_count") or 1)
        except (TypeError, ValueError):
            hostile_count = 1
        count = max(1, hostile_count)
        existing = current_by_system.get(system)
        if existing is not None:
            existing["hostile_count"] = int(existing["hostile_count"]) + count
            existing["active_hostile_count"] = int(
                existing["active_hostile_count"]
            ) + count
            continue
        previous = previous_by_system.get(system, {})
        current_by_system[system] = {
            "system_name": system,
            "hostile_count": count,
            "active_hostile_count": count,
            "created_at": first_seen_by_system.get(system)
            or str(previous.get("created_at") or item.get("last_seen_at") or ""),
            "active": True,
        }

    for system in monitoring_systems:
        if system in current_by_system:
            continue
        previous = previous_by_system.get(system, {})
        current_by_system[system] = {
            "system_name": system,
            "hostile_count": 0,
            "active_hostile_count": 0,
            "created_at": str(previous.get("created_at") or ""),
            "active": False,
        }

    ordered: list[dict[str, Any]] = []
    for item in summaries:
        system = str(item.get("system_name") or "Unknown")
        current = current_by_system.pop(system, None)
        if current is not None:
            ordered.append(current)
    ordered.extend(
        sorted(
            current_by_system.values(),
            key=lambda item: str(item.get("created_at") or ""),
        )
    )
    return ordered


def build_heartbeat_details(
    last_action: str,
    last_error: str = "",
    client_version: str = "",
    host: str = "",
    last_success_at: str = "",
) -> dict[str, object]:
    """Return the alert-client heartbeat details for status views."""
    details: dict[str, object] = {
        "mode": "events",
        "transport": "events",
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
    return details


def play_alert_sound(volume: float = 1.0) -> None:
    """Play the bundled alert sound once if the resource exists."""
    sound_path = Path(__file__).parent.parent / "resources" / "alert.wav"
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


def _severity_rank(value: str) -> int:
    return {
        "low": 0,
        "medium": 1,
        "high": 2,
        "critical": 3,
    }.get(str(value or "").casefold(), 2)


def _in_quiet_hours(value: str, now: datetime | None = None) -> bool:
    """Return whether local time falls inside a HH:MM-HH:MM window."""
    text = str(value or "").strip()
    if not text or "-" not in text:
        return False
    start_text, end_text = (part.strip() for part in text.split("-", 1))
    try:
        start = datetime.strptime(start_text, "%H:%M").time()
        end = datetime.strptime(end_text, "%H:%M").time()
    except ValueError:
        return False
    current = (now or datetime.now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class AlertOverlay(QWidget):
    """Always-on-top compact alert overlay."""

    ACTIVE_SOUNDS: list[QSoundEffect] = []

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QFrame, QLabel, QLabel, QLabel]] = []
        self._status = QLabel("连接中")
        self._status.setObjectName("statusLabel")
        self._title = QLabel("EVE SENTRY")
        self._title.setObjectName("titleLabel")
        self._content_layout: QVBoxLayout | None = None
        self._row_layout: QGridLayout | None = None
        self._scroll: QScrollArea | None = None
        self._anchor_rect: dict[str, Any] | None = None
        self._tile_width = OVERLAY_TILE_WIDTH
        self._tile_height = OVERLAY_TILE_HEIGHT
        self._drag_position: QPoint | None = None
        self._resize_edges = 0
        self._resize_start_geometry: QRect | None = None
        self._resize_start_position: QPoint | None = None
        self._user_positioned = False
        self._user_resized = False
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
        self._content_layout = layout
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self._title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._title.setMouseTracking(True)
        self._status.setMouseTracking(True)
        self._title.installEventFilter(self)
        self._status.installEventFilter(self)
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._status)
        layout.addLayout(header)

        row_container = QWidget()
        row_container.setObjectName("rowContainer")
        row_container.setMouseTracking(True)
        row_container.installEventFilter(self)
        self._row_layout = QGridLayout(row_container)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setHorizontalSpacing(OVERLAY_GRID_SPACING)
        self._row_layout.setVerticalSpacing(OVERLAY_GRID_SPACING)
        self._row_layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )

        scroll = QScrollArea()
        scroll.setObjectName("alertScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(row_container)
        scroll.setVisible(False)
        scroll.setMouseTracking(True)
        visible_tile_rows = max(1, MAX_OVERLAY_ROWS // OVERLAY_TILE_COLUMNS)
        scroll.setMaximumHeight(
            self._tile_height * visible_tile_rows
            + OVERLAY_GRID_SPACING * max(0, visible_tile_rows - 1)
        )
        scroll.viewport().setMouseTracking(True)
        scroll.viewport().installEventFilter(self)
        layout.addWidget(scroll)
        self._scroll = scroll

        self.setStyleSheet(
            """
            QFrame#overlayFrame {
                background: rgba(4, 12, 18, 210);
                border: 1px solid rgba(92, 213, 238, 150);
                border-radius: 8px;
            }
            QLabel#titleLabel {
                color: #8bdaf1;
                letter-spacing: 0;
            }
            QLabel#statusLabel {
                color: #9fb7c3;
                font-size: 11px;
            }
            QFrame#alertRow {
                background: rgba(111, 25, 22, 190);
                border: 1px solid #ff6b5f;
                border-radius: 4px;
            }
            QFrame#alertRow QLabel {
                color: #fff7ea;
                background: transparent;
                border: 0;
            }
            QFrame#alertRow[hostile="false"] {
                background: rgba(16, 91, 61, 185);
                border: 1px solid #4fd19a;
            }
            QFrame#alertRow[hostile="false"] QLabel {
                color: #d9fff0;
            }
            QLabel#systemCell {
                font-size: 15px;
                font-weight: 700;
            }
            QLabel#hostileCell, QLabel#stateCell {
                font-size: 12px;
                font-weight: 600;
            }
            QScrollArea#alertScroll {
                background: transparent;
                border: 0;
            }
            QScrollArea#alertScroll QWidget#rowContainer {
                background: transparent;
            }
            """
        )

    def set_status(self, text: str, tone: str = "idle") -> None:
        color = {
            "ok": "#6ee7b7",
            "warn": "#f5c96b",
            "danger": "#ff8b7f",
            "idle": "#9fb7c3",
        }.get(tone, "#9fb7c3")
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color};")

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
                self._user_resized = True
                self._user_positioned = True
                self._title.setAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                )
                self._status.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
                )
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
                event.accept()
                return True
            if self._drag_position is not None:
                self._user_positioned = True
            self._drag_position = None
            event.accept()
            return True
        return False

    def _resize_edges_at(self, global_position: QPoint) -> int:
        geometry = self.frameGeometry()
        margin = OVERLAY_RESIZE_MARGIN
        edges = 0
        if abs(global_position.x() - geometry.left()) <= margin:
            edges |= RESIZE_LEFT
        elif abs(global_position.x() - geometry.right()) <= margin:
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._user_resized:
            self._layout_rows_for_size()

    def show_summaries(self, summaries: list[dict[str, Any]]) -> None:
        screen = self._screen_for_anchor()
        if screen is not None:
            self._apply_screen_metrics(screen)
        rows = aggregate_alert_summaries(summaries)
        self._ensure_row_count(len(rows))
        for index, (frame, system_label, hostile_label, state_label) in enumerate(
            self._rows
        ):
            if index >= len(rows):
                frame.setVisible(False)
                system_label.setText("")
                hostile_label.setText("")
                state_label.setText("")
                continue
            item = rows[index]
            active = bool(item.get("active", True))
            hostile_count = max(
                0,
                int(item.get("active_hostile_count") or 0) if active else 0,
            )
            system_label.setText(str(item["system_name"]))
            hostile_label.setText(f"敌 {hostile_count}")
            state_label.setText("来敌" if hostile_count > 0 else "安全")
            frame.setProperty("hostile", "true" if hostile_count > 0 else "false")
            frame.style().unpolish(frame)
            frame.style().polish(frame)
            for label in (system_label, hostile_label, state_label):
                label.style().unpolish(label)
                label.style().polish(label)
            frame.setVisible(True)
        self._layout_rows_for_size()
        self._resize_to_content()
        if not self._user_positioned:
            self.move_to_default_position()

    def _resize_to_content(self) -> None:
        """Resize both larger and smaller when the visible tile count changes."""
        if self._user_resized:
            return
        if self._content_layout is not None:
            self._content_layout.invalidate()
            self._content_layout.activate()
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        hint = self.sizeHint()
        self.resize(
            max(self.minimumWidth(), hint.width()),
            max(self.minimumHeight(), hint.height()),
        )

    def _ensure_row_count(self, count: int) -> None:
        if self._row_layout is None:
            return
        while len(self._rows) < count:
            frame = QFrame()
            frame.setObjectName("alertRow")
            frame.setVisible(False)
            frame.setFixedSize(self._tile_width, self._tile_height)
            frame.setProperty("hostile", "true")
            frame.setMouseTracking(True)
            frame.installEventFilter(self)

            tile_layout = QVBoxLayout(frame)
            tile_layout.setContentsMargins(9, 7, 9, 7)
            tile_layout.setSpacing(2)
            system_label = QLabel("")
            system_label.setObjectName("systemCell")
            system_label.setFixedWidth(self._tile_width - 18)
            system_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            system_label.setMouseTracking(True)
            system_label.installEventFilter(self)
            tile_layout.addWidget(system_label)

            detail_layout = QHBoxLayout()
            detail_layout.setContentsMargins(0, 0, 0, 0)
            detail_layout.setSpacing(4)
            hostile_label = QLabel("")
            hostile_label.setObjectName("hostileCell")
            hostile_label.setFixedWidth(OVERLAY_HOSTILE_COUNT_WIDTH)
            hostile_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            hostile_label.setMouseTracking(True)
            hostile_label.installEventFilter(self)
            state_label = QLabel("")
            state_label.setObjectName("stateCell")
            state_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            state_label.setMouseTracking(True)
            state_label.installEventFilter(self)
            detail_layout.addWidget(hostile_label)
            detail_layout.addWidget(state_label)
            detail_layout.addStretch(1)
            tile_layout.addLayout(detail_layout)

            tile_index = len(self._rows)
            self._row_layout.addWidget(
                frame,
                tile_index // OVERLAY_TILE_COLUMNS,
                tile_index % OVERLAY_TILE_COLUMNS,
            )
            self._rows.append(
                (frame, system_label, hostile_label, state_label)
            )

    def _layout_rows_for_size(self) -> None:
        if self._row_layout is None or self._scroll is None:
            return
        visible_rows = [row for row in self._rows if not row[0].isHidden()]
        for frame, *_labels in self._rows:
            self._row_layout.removeWidget(frame)
        if not visible_rows:
            self._scroll.setVisible(False)
            self._scroll.setMinimumHeight(0)
            self._scroll.setMaximumHeight(16777215)
            return

        if self._user_resized:
            available_width = max(1, self.width() - 28)
            columns = max(
                1,
                min(
                    len(visible_rows),
                    (available_width + OVERLAY_GRID_SPACING)
                    // (self._tile_width + OVERLAY_GRID_SPACING),
                ),
            )
        else:
            columns = max(1, min(OVERLAY_TILE_COLUMNS, len(visible_rows)))

        for index, (frame, *_labels) in enumerate(visible_rows):
            self._row_layout.addWidget(
                frame,
                index // columns,
                index % columns,
            )
        row_count = max(1, (len(visible_rows) + columns - 1) // columns)
        self._scroll.setVisible(True)
        if self._user_resized:
            self._scroll.setMinimumHeight(0)
            self._scroll.setMaximumHeight(16777215)
        else:
            visible_row_limit = max(1, (MAX_OVERLAY_ROWS + columns - 1) // columns)
            visible_row_count = min(row_count, visible_row_limit)
            content_height = (
                self._tile_height * visible_row_count
                + OVERLAY_GRID_SPACING * max(0, visible_row_count - 1)
            )
            self._scroll.setFixedHeight(content_height)
        self._row_layout.invalidate()

    def set_anchor_rect(self, rect: dict[str, Any] | None) -> None:
        """Anchor automatic placement to the display hosting an EVE window."""
        self._anchor_rect = dict(rect) if rect else None
        screen = self._screen_for_anchor()
        if screen is not None:
            self._apply_screen_metrics(screen)
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

    def _apply_screen_metrics(self, screen) -> None:
        geometry = screen.availableGeometry()
        self._tile_width, self._tile_height = overlay_tile_dimensions(
            geometry.width(),
            geometry.height(),
        )
        self.setMinimumSize(OVERLAY_MIN_WIDTH, OVERLAY_MIN_HEIGHT)
        for frame, system_label, _hostile_label, _state_label in self._rows:
            frame.setFixedSize(self._tile_width, self._tile_height)
            system_label.setFixedWidth(self._tile_width - 18)
        self._layout_rows_for_size()

    def move_to_default_position(self) -> None:
        screen = self._screen_for_anchor()
        if screen is None:
            return
        self._apply_screen_metrics(screen)
        geometry = screen.availableGeometry()
        self._resize_to_content()
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
        self.client_id = client_id or f"alert-client:{os.getpid()}"
        self.api_key = str(api_key or "").strip()
        self.api_factory = api_factory
        self.consumer = AlertEventConsumer(state)
        self._stop_requested = False
        self._last_heartbeat_at = 0.0
        self._last_success_at = ""
        self._runtime = resolve_runtime_identity()

    def stop(self) -> None:
        """Request the worker to stop after the current network call returns."""
        self._stop_requested = True

    def run(self) -> None:
        api = self.api_factory(
            self.server,
            timeout=min(3.0, self.timeout),
            api_key=self.api_key,
        )
        backoff = 1.0
        while not self._stop_requested:
            retrying_after_error = False
            try:
                connection_announced = False
                for event in api.iter_events(
                    timeout=self.timeout,
                    heartbeat=1.0,
                    should_stop=lambda: self._stop_requested,
                    include_bootstrap=True,
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
                        self.bootstrap_received.emit(data)
                        self._post_heartbeat(api, "connected")
                        continue
                    if event_name == "safe" and isinstance(data, dict):
                        self.safe_received.emit(data)
                        self._post_heartbeat(api, "safe:1", force=True)
                        continue
                    if event_name != "alert" or not isinstance(data, dict):
                        self._post_heartbeat(api, "connected")
                        continue
                    alert = data
                    if self.consumer.accept(alert):
                        self.alert_received.emit(alert)
                        self._post_heartbeat(api, "alert:1", force=True)
                    else:
                        self._post_heartbeat(api, "connected")
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
        if not force and now < self._last_heartbeat_at + self.heartbeat_interval:
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
                ),
            )
            self._last_heartbeat_at = now
        except IntelApiError as exc:
            logger.warning("Heartbeat update failed: %s", exc)


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
    ) -> None:
        self.app = app
        self.args = args
        self.state = AlertClientState(args.state)
        self.state.load_seen_ids()
        self.api_factory = api_factory
        self._tray_enabled = bool(tray_enabled)
        self._notification_callback = notification_callback
        self.overlay = AlertOverlay()
        self.overlay.set_status("连接中", "warn")
        self.overlay.show()
        self.overlay.move_to_default_position()
        self._recent_summaries: list[dict[str, Any]] = []
        self._local_hostile_counts: dict[str, tuple[str, int]] = {}
        self._alert_volume = max(0.0, min(1.0, float(getattr(args, "alert_volume", 1.0))))
        self._alert_muted = bool(getattr(args, "alert_muted", False))
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

    def start(self) -> None:
        """Start the tray icon and SSE worker."""
        if self._tray is not None:
            self._tray.show()
        self._worker.start()

    def stop(self, *, wait_for_worker: bool = True) -> None:
        """Request shutdown, optionally waiting for the SSE worker to exit."""
        sound_timer = getattr(self, "_sound_repeat_timer", None)
        if sound_timer is not None:
            sound_timer.stop()
        self._remaining_sound_plays = 0
        self._worker.stop()
        self.overlay.hide()
        if self._tray is not None:
            self._tray.hide()
        if wait_for_worker and self._worker.isRunning():
            self._worker.wait(int((self.args.timeout + 4.0) * 1000))

    def is_running(self) -> bool:
        """Return whether the SSE worker is still unwinding."""
        return self._worker.isRunning()

    def show_monitoring_systems(self, system_names: list[str]) -> None:
        """Ensure monitored systems are visible before the first SSE refresh."""
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
        self.overlay.show_summaries(self._recent_summaries)

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
        for key in system_keys:
            local_counts.pop(key, None)
        self._recent_summaries = [
            item
            for item in self._recent_summaries
            if str(item.get("system_name") or "Unknown").casefold()
            not in system_keys
        ]
        self.overlay.show_summaries(self._recent_summaries)

    def set_anchor_window(self, window: dict[str, Any] | None) -> None:
        """Place the embedded overlay on the EVE window's display."""
        self.overlay.set_anchor_rect(window)

    def update_local_hostile_count(self, system_name: str, count: int) -> None:
        """Apply authoritative red-icon evidence from this monitor process."""
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
                }
            )
            return
        local_counts.pop(key, None)
        self._on_safe(
            {
                "system_name": system,
                "hostile_count": 0,
                "message": f"✅ {system} 清空",
            }
        )

    def _apply_local_hostile_counts(self) -> None:
        """Keep local visual counts from being reduced by delayed server state."""
        for system, hostile_count in getattr(
            self, "_local_hostile_counts", {}
        ).values():
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
        self.overlay.move_to_default_position()

    def _restart_worker(self) -> None:
        if self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(int((self.args.timeout + 4.0) * 1000))
        self._worker = AlertEventWorker(
            self.args.server,
            self.state,
            timeout=self.args.timeout,
            heartbeat_interval=self.args.heartbeat_interval,
            reconnect_max_delay=self.args.reconnect_max_delay,
            api_factory=self.api_factory,
        )
        self._worker.alert_received.connect(self._on_alert)
        self._worker.safe_received.connect(self._on_safe)
        self._worker.bootstrap_received.connect(self._on_bootstrap)
        self._worker.status_changed.connect(self._on_status)
        self._worker.start()

    def _on_status(self, status: str, message: str) -> None:
        if status == "connected":
            self.overlay.set_status("SSE 在线", "ok")
        elif status == "reconnecting":
            self.overlay.set_status("重连中", "warn")
        elif status == "error":
            self.overlay.set_status("连接异常", "danger")
            if message:
                self._notify("EVE Sentry Alert", message)
        else:
            self.overlay.set_status(status, "idle")

    def _on_alert(self, alert: dict[str, Any]) -> None:
        summary = summarize_alert(alert)
        summary["active_hostile_count"] = summary["hostile_count"]
        system = str(summary.get("system_name") or "Unknown")
        existing = next(
            (
                item
                for item in self._recent_summaries
                if str(item.get("system_name") or "Unknown") == system
            ),
            None,
        )
        if existing is None:
            self._recent_summaries.append(summary)
            self._recent_summaries = self._recent_summaries[-50:]
        else:
            existing.update(summary)
        self._apply_local_hostile_counts()
        self.overlay.show_summaries(self._recent_summaries)
        self.overlay.set_status("新告警", "danger")
        hostile_count = int(summary.get("hostile_count") or 0)
        notification_key = system.casefold()
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
        if not muted and not in_cooldown:
            self._play_alert_sound_sequence()
        if in_cooldown:
            return
        last_notified[notification_key] = (now, hostile_count)
        self._notify(
            "敌对告警",
            f"❗ {summary['system_name']} 来敌 {hostile_count} 人",
        )

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
        if volume is None:
            play_alert_sound()
        else:
            play_alert_sound(volume)
        remaining -= 1
        self._remaining_sound_plays = remaining
        timer = getattr(self, "_sound_repeat_timer", None)
        if remaining > 0 and timer is not None:
            timer.start(int(getattr(self, "_alert_repeat_interval_ms", 2000)))

    def _on_safe(self, event: dict[str, Any]) -> None:
        """Notify once after the final hostile leaves a solar system."""
        system_name = str(
            event.get("system_name") or event.get("system") or "Unknown"
        ).strip() or "Unknown"
        if system_name.casefold() in getattr(self, "_local_hostile_counts", {}):
            return
        for item in self._recent_summaries:
            if str(item.get("system_name") or "Unknown") != system_name:
                continue
            item["active"] = False
            item["hostile_count"] = 0
            item["active_hostile_count"] = 0
        self.overlay.show_summaries(self._recent_summaries)
        self.overlay.set_status("星系安全", "ok")
        message = str(event.get("message") or "").strip()
        if not message:
            message = f"✅ {system_name} 清空"
        self._notify("星系安全", message)

    def _on_bootstrap(self, bootstrap: dict[str, Any]) -> None:
        self._recent_summaries = sync_alert_summaries_from_bootstrap(
            self._recent_summaries,
            bootstrap,
        )
        self._apply_local_hostile_counts()
        self.overlay.show_summaries(self._recent_summaries)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
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
