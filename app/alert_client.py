"""Local tray alert client that consumes server-side SSE alerts only."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QTimer, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.core.heartbeat import (
    heartbeat_now_iso,
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
    }


def aggregate_alert_summaries(
    summaries: list[dict[str, Any]],
    max_rows: int = MAX_OVERLAY_ROWS,
) -> list[dict[str, Any]]:
    """Aggregate recent alert summaries by system for compact display."""
    by_system: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in summaries:
        system = str(item.get("system_name") or "Unknown")
        hostile_count = int(item.get("hostile_count") or 1)
        existing = by_system.get(system)
        if existing is None:
            by_system[system] = {
                "system_name": system,
                "hostile_count": hostile_count,
                "created_at": str(item.get("created_at") or ""),
            }
        else:
            existing["hostile_count"] = int(existing["hostile_count"]) + hostile_count
            existing["created_at"] = str(item.get("created_at") or existing["created_at"])
            by_system.move_to_end(system)
    ordered = list(by_system.values())
    ordered.sort(
        key=lambda item: (int(item.get("hostile_count") or 0), str(item.get("created_at") or "")),
        reverse=True,
    )
    return ordered[:max(1, max_rows)]


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


def play_alert_sound() -> None:
    """Play the bundled alert sound once if the resource exists."""
    sound_path = Path(__file__).parent.parent / "resources" / "alert.wav"
    if not sound_path.exists():
        return
    try:
        sound = QSoundEffect()
        sound.setSource(QUrl.fromLocalFile(str(sound_path.resolve())))
        sound.setVolume(1.0)
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[QLabel] = []
        self._status = QLabel("连接中")
        self._status.setObjectName("statusLabel")
        self._title = QLabel("EVE SENTRY")
        self._title.setObjectName("titleLabel")
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("EVE Sentry Alert")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(260)

        frame = QFrame()
        frame.setObjectName("overlayFrame")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(frame)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self._title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._status)
        layout.addLayout(header)

        for _index in range(MAX_OVERLAY_ROWS):
            label = QLabel("")
            label.setObjectName("alertRow")
            label.setVisible(False)
            label.setMinimumHeight(28)
            layout.addWidget(label)
            self._rows.append(label)

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
            QLabel#alertRow {
                color: #fff7ea;
                background: rgba(117, 29, 22, 145);
                border: 1px solid rgba(255, 112, 88, 150);
                border-radius: 5px;
                padding: 4px 8px;
                font-size: 18px;
                font-weight: 700;
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

    def show_summaries(self, summaries: list[dict[str, Any]]) -> None:
        rows = aggregate_alert_summaries(summaries, max_rows=len(self._rows))
        for index, label in enumerate(self._rows):
            if index >= len(rows):
                label.setVisible(False)
                label.setText("")
                continue
            item = rows[index]
            label.setText(f"{item['system_name']}  敌:{item['hostile_count']}")
            label.setVisible(True)
        self.adjustSize()
        self.move_to_default_position()

    def move_to_default_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.adjustSize()
        x = geometry.right() - self.width() - 28
        y = geometry.top() + 88
        self.move(max(0, x), max(0, y))


class AlertEventWorker(QThread):
    """Background SSE consumer for server-side alert events."""

    alert_received = pyqtSignal(dict)
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
        api_factory: Callable[..., IntelApiClient] = IntelApiClient,
    ) -> None:
        super().__init__()
        self.server = server
        self.state = state
        self.timeout = max(1.0, float(timeout))
        self.heartbeat_interval = max(5.0, float(heartbeat_interval))
        self.reconnect_max_delay = max(1.0, float(reconnect_max_delay))
        self.client_id = client_id or f"alert-client:{os.getpid()}"
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
        api = self.api_factory(self.server, timeout=min(3.0, self.timeout))
        backoff = 1.0
        self._post_heartbeat(api, "starting")
        while not self._stop_requested:
            try:
                self.status_changed.emit("connected", "")
                self._post_heartbeat(api, "connected", force=True)
                for alert in api.iter_alert_events(timeout=self.timeout):
                    if self._stop_requested:
                        break
                    self._last_success_at = heartbeat_now_iso()
                    if self.consumer.accept(alert):
                        self.alert_received.emit(alert)
                        self._post_heartbeat(api, "alert:1", force=True)
                    else:
                        self._post_heartbeat(api, "connected")
                backoff = 1.0
            except IntelApiError as exc:
                message = summarize_heartbeat_error(str(exc))
                self.status_changed.emit("error", message)
                self._post_heartbeat(api, "error", message, force=True)
                self._sleep_with_stop(backoff)
                backoff = min(self.reconnect_max_delay, backoff * 2)
            except Exception as exc:
                message = summarize_heartbeat_error(str(exc))
                self.status_changed.emit("error", message)
                self._post_heartbeat(api, "error", message, force=True)
                self._sleep_with_stop(backoff)
                backoff = min(self.reconnect_max_delay, backoff * 2)
            if not self._stop_requested:
                self.status_changed.emit("reconnecting", "")
                self._post_heartbeat(api, "reconnecting")

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
    ) -> None:
        self.app = app
        self.args = args
        self.state = AlertClientState(args.state)
        self.state.load_seen_ids()
        self.overlay = AlertOverlay()
        self.overlay.set_status("连接中", "warn")
        self.overlay.show()
        self.overlay.move_to_default_position()
        self._recent_summaries: list[dict[str, Any]] = []
        self._tray = QSystemTrayIcon(self.overlay)
        self._worker = AlertEventWorker(
            args.server,
            self.state,
            timeout=args.timeout,
            heartbeat_interval=args.heartbeat_interval,
            reconnect_max_delay=args.reconnect_max_delay,
            api_factory=api_factory,
        )
        self._setup_tray()
        self._worker.alert_received.connect(self._on_alert)
        self._worker.status_changed.connect(self._on_status)

    def start(self) -> None:
        """Start the tray icon and SSE worker."""
        self._tray.show()
        self._worker.start()

    def stop(self) -> None:
        """Stop the worker and hide the tray icon."""
        self._worker.stop()
        self._worker.wait(int((self.args.timeout + 4.0) * 1000))
        self._tray.hide()

    def _setup_tray(self) -> None:
        icon = self.overlay.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        self.overlay.setWindowIcon(icon)
        self._tray.setIcon(icon)
        self._tray.setToolTip("EVE Sentry Alert")
        self._tray.activated.connect(self._on_tray_activated)

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
        self._tray.setContextMenu(menu)

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
        )
        self._worker.alert_received.connect(self._on_alert)
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
                self._tray.showMessage("EVE Sentry Alert", message)
        else:
            self.overlay.set_status(status, "idle")

    def _on_alert(self, alert: dict[str, Any]) -> None:
        summary = summarize_alert(alert)
        self._recent_summaries.append(summary)
        self._recent_summaries = self._recent_summaries[-50:]
        self.overlay.show_summaries(self._recent_summaries)
        self.overlay.set_status("新告警", "danger")
        play_alert_sound()
        self._tray.showMessage(
            "EVE Sentry Alert",
            f"{summary['system_name']}  敌:{summary['hostile_count']}",
        )


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
