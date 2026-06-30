"""Main application window."""

import logging
import os
import time
from datetime import datetime

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.engine.capturer import Capturer
from app.engine.detector import Detector
from app.engine.ocr import OCREngine
from app.engine.worker import MonitorWorker
from app.intel_client import IntelApiClient, IntelApiError
from app.models.region_prefs import RegionPreferences
from app.models.whitelist import Whitelist
from app.ui.alert_dialog import AlertDialog
from app.ui.region_selector import RegionSelector
from app.ui.settings import SettingsPanel

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(700, 450)

        self._whitelist = Whitelist("whitelist.json")
        self._region_prefs = RegionPreferences("region_prefs.json")
        self._capturer = Capturer()
        self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
        self._detector = Detector(self._whitelist, cooldown_seconds=60.0)
        self._worker: MonitorWorker | None = None
        self._intel_url = os.environ.get(
            "EVE_SENTRY_INTEL_URL",
            "http://127.0.0.1:8765",
        ).strip()
        configured_system = os.environ.get("EVE_SENTRY_SYSTEM", "").strip()
        self._intel_system = configured_system or "Unknown"
        self._intel_system_id: int | None = None
        self._intel_system_source = "env" if configured_system else "default"
        self._use_esi_location = _env_flag(
            "EVE_SENTRY_USE_ESI_LOCATION",
            default=not bool(configured_system),
        )
        self._esi_location_ttl = _env_float(
            "EVE_SENTRY_ESI_LOCATION_TTL",
            default=30.0,
            minimum=1.0,
        )
        self._esi_location_next_check = 0.0
        self._last_esi_location_error = ""
        self._intel_client = self._create_intel_client()

        self._popup_alerts_enabled = (
            os.environ.get("EVE_SENTRY_SHOW_POPUPS", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self._alert_visible = False
        self._alert_queue: list[list[str]] = []
        self._manual_region: dict | None = None
        self._detected_region: dict | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._settings = SettingsPanel(self._whitelist)
        self._settings.setFixedWidth(220)
        root.addWidget(self._settings)

        right = QVBoxLayout()
        right.setSpacing(6)

        self._monitor_btn = QPushButton("Start Monitor")
        self._monitor_btn.setMinimumHeight(40)
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #228b22; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #2ea62e; }"
            "QPushButton:checked { background: #cc0000; }"
        )
        self._monitor_btn.setCheckable(True)
        self._monitor_btn.clicked.connect(self._toggle_monitor)
        right.addWidget(self._monitor_btn)

        self._window_combo = QComboBox()
        self._window_combo.setStyleSheet("font-size: 11px;")
        self._window_combo.currentIndexChanged.connect(self._on_window_selected)
        right.addWidget(self._window_combo)

        self._window_label = QLabel("Window: not detected")
        self._window_label.setStyleSheet("color: #666; font-size: 11px;")
        right.addWidget(self._window_label)

        right.addWidget(QLabel("Status Log:"))

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            "QTextEdit { background: #1a1a2e; color: #e0e0e0; "
            "font-family: Consolas, monospace; font-size: 12px; }"
        )
        right.addWidget(self._log)

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self._log.clear)
        btn_row.addWidget(clear_btn)

        select_btn = QPushButton("Select Member List")
        select_btn.clicked.connect(self._select_region)
        btn_row.addWidget(select_btn)

        btn_row.addStretch()
        right.addLayout(btn_row)

        root.addLayout(right, 1)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status_label = QLabel("Idle")
        self._status.addWidget(self._status_label)

        self._setup_tray()
        self._detect_window()

    def _detect_window(self) -> None:
        """Find all EVE windows and populate the window selector."""
        keyword = self._settings.get_keyword()
        windows = self._capturer.list_eve_windows(keyword)
        self._window_combo.blockSignals(True)
        self._window_combo.clear()
        if windows:
            for window in windows:
                self._window_combo.addItem(window["title"], window["hwnd"])
        self._window_combo.blockSignals(False)

        if windows:
            self._window_combo.setCurrentIndex(0)
            self._on_window_selected(0)
            self._log_message(f"Found {len(windows)} EVE window(s)")
        else:
            self._detected_region = None
            self._capturer.close()
            self._window_label.setText("Window: not found")

    def _current_window_info(self) -> dict | None:
        """Return the currently selected EVE window info."""
        hwnd = self._window_combo.currentData()
        if hwnd is None:
            return None
        info = self._capturer.get_window_info(hwnd)
        if info is not None:
            return info
        keyword = self._settings.get_keyword()
        windows = self._capturer.list_eve_windows(keyword)
        return next((window for window in windows if window["hwnd"] == hwnd), None)

    def _on_window_selected(self, index: int) -> None:
        """Update the detected region when the user picks an EVE window."""
        if index < 0:
            return

        info = self._current_window_info()
        title = self._window_combo.currentText()
        if info is None:
            self._detected_region = None
            self._window_label.setText("Window: stale selection, re-detect needed")
            return

        self._capturer.select_window(
            info["hwnd"],
            info["title"],
            info["w"],
            info["h"],
            start_capture=False,
        )
        member = self._region_prefs.resolve_region(info)
        if member is None:
            member = self._capturer.get_member_list_region(info)
        self._detected_region = member
        self._window_label.setText(
            f"Window: {title} -> member list {member['w']}x{member['h']}"
        )

    def _select_region(self) -> None:
        """Show overlay on top of EVE window for drag-to-select region."""
        hwnd = self._window_combo.currentData()
        if hwnd is not None:
            self._capturer.activate_window(hwnd)

        info = self._current_window_info()
        if info is None:
            info = self._capturer.find_eve_window(keyword=self._settings.get_keyword())
        if info is None:
            QMessageBox.critical(self, "Error", "EVE window not found.")
            return

        self._capturer.activate_window(info["hwnd"])
        info = self._capturer.get_window_info(info["hwnd"]) or info

        self._capturer.select_window(
            info["hwnd"],
            info["title"],
            info["w"],
            info["h"],
            start_capture=False,
        )
        self._log_message(
            f"Selecting region on {info['title']} at ({info['x']},{info['y']}) "
            f"{info['w']}x{info['h']}"
        )

        self.hide()
        self._selector = RegionSelector(info["x"], info["y"], info["w"], info["h"])
        self._selector.region_selected.connect(self._on_region_selected)
        self._selector.selector_closed.connect(self._on_selector_closed)
        self._selector.show()

    def _on_region_selected(self, x: int, y: int, w: int, h: int) -> None:
        """Handle region selected; coordinates are absolute screen coords."""
        self._manual_region = {"x": x, "y": y, "w": w, "h": h}
        window = self._current_window_info()
        if window is not None:
            self._region_prefs.save_region(window, self._manual_region)
        self._window_label.setText(f"Manual region: ({x},{y}) {w}x{h}")
        self._log_message(f"Saved member-list region {w}x{h} @ ({x},{y})")
        self.show()

    def _on_selector_closed(self) -> None:
        """Restore the main window when the selector closes or is cancelled."""
        self.show()
        self.raise_()

    def _toggle_monitor(self, checked: bool) -> None:
        if checked:
            self._start_monitor()
        else:
            self._stop_monitor()

    def _start_monitor(self) -> None:
        if self._detected_region is None:
            self._detect_window()

        region = self._manual_region or self._detected_region
        window = self._current_window_info()
        if window is None:
            self._detect_window()
            region = self._manual_region or self._detected_region
            window = self._current_window_info()

        if region is None:
            QMessageBox.critical(self, "Error", "No capture region is configured.")
            self._monitor_btn.setChecked(False)
            return
        if window is None:
            QMessageBox.critical(self, "Error", "No EVE window is available.")
            self._monitor_btn.setChecked(False)
            return

        self._refresh_intel_location(force=True)

        if self._worker is not None:
            if self._worker.isRunning():
                self._log_message("Stopping previous monitor thread...")
                self._worker.stop()
                if not self._worker.wait(5000):
                    logger.warning("Old worker thread did not stop within 5 s")
                    self._monitor_btn.setChecked(False)
                    QMessageBox.critical(
                        self,
                        "Error",
                        "Failed to stop the previous monitor thread.",
                    )
                    return
                self._log_message("Previous monitor thread stopped")
            self._disconnect_worker_signals()

        self._worker = MonitorWorker(self._capturer, self._ocr, self._detector)
        self._worker.set_window(window)
        self._worker.set_region(region["x"], region["y"], region["w"], region["h"])
        self._worker.set_interval(self._settings.get_interval())
        self._worker.threat_detected.connect(self._on_threat_detected)
        self._worker.status_update.connect(self._log_message)
        self._worker.scan_complete.connect(self._update_scan_count)
        self._worker.start()

        self._monitor_btn.setText("Stop Monitor")
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #cc0000; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #ee2222; }"
        )
        self._status_label.setText("Running")
        self._status_label.setStyleSheet("color: #228b22; font-weight: bold;")
        self._log_message("Monitor started")

    def _disconnect_worker_signals(self) -> None:
        """Safely disconnect all signals from the current worker."""
        try:
            self._worker.threat_detected.disconnect()
        except TypeError:
            pass
        try:
            self._worker.status_update.disconnect()
        except TypeError:
            pass
        try:
            self._worker.scan_complete.disconnect()
        except TypeError:
            pass

    def _stop_monitor(self) -> None:
        if self._worker:
            self._worker.stop()
            if not self._worker.wait(3000):
                logger.warning("Worker thread did not stop within 3 s timeout")
                self._disconnect_worker_signals()
                self._capturer = Capturer()
                self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
                self._detector = Detector(self._whitelist, cooldown_seconds=60.0)
            self._worker = None

        self._monitor_btn.setText("Start Monitor")
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #228b22; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #2ea62e; }"
        )
        self._status_label.setText("Stopped")
        self._status_label.setStyleSheet("color: #888;")
        self._log_message("Monitor stopped")

    def _on_threat(self, threats: list[str]) -> None:
        """Show non-blocking alert dialog when threats are detected."""
        if not self._popup_alerts_enabled:
            return

        if self._alert_visible:
            self._alert_queue.append(threats)
            return

        self._alert_visible = True
        dialog = AlertDialog(threats, self)
        dialog.finished.connect(self._on_alert_closed)
        dialog.show()

    def _on_threat_detected(self, threats: list[str]) -> None:
        """Publish detected threats, then optionally show local UI alerts."""
        self._publish_intel(threats)
        self._on_threat(threats)

    def _create_intel_client(self) -> IntelApiClient | None:
        enabled = (
            os.environ.get("EVE_SENTRY_PUBLISH_INTEL", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if not enabled or not self._intel_url:
            return None
        timeout_raw = os.environ.get("EVE_SENTRY_INTEL_TIMEOUT", "1.0")
        try:
            timeout = max(0.1, float(timeout_raw))
        except ValueError:
            timeout = 1.0
        return IntelApiClient(self._intel_url, timeout=timeout)

    def _publish_intel(self, threats: list[str]) -> None:
        if self._intel_client is None or not threats:
            return

        self._refresh_intel_location()
        source = "eve-sentry-detector"
        window_title = self._window_combo.currentText()
        metadata = {"system_source": self._intel_system_source}
        if window_title:
            metadata["window_title"] = window_title
        try:
            created = self._intel_client.post_observation(
                system_name=self._intel_system or "Unknown",
                system_id=self._intel_system_id,
                names=threats,
                source=source,
                raw_text=", ".join(threats),
                metadata=metadata,
            )
        except IntelApiError as exc:
            self._log_message(f"情报上报失败: {exc}")
            return

        observation_id = created.get("observation", {}).get("id", "")
        suffix = f" ({observation_id[:8]})" if observation_id else ""
        self._log_message(f"已上报情报: {len(threats)} 个目标{suffix}")

    def _refresh_intel_location(self, force: bool = False) -> bool:
        if (
            not self._use_esi_location
            or self._intel_client is None
        ):
            return False

        now = time.monotonic()
        if not force and now < self._esi_location_next_check:
            return bool(self._intel_system_id)
        self._esi_location_next_check = now + self._esi_location_ttl

        try:
            system = self._intel_client.current_esi_system()
        except IntelApiError as exc:
            message = str(exc)
            if message != self._last_esi_location_error:
                self._last_esi_location_error = message
                self._log_message(f"ESI current-system sync unavailable: {message}")
            return False

        if not system:
            message = "location did not include a solar system"
            if message != self._last_esi_location_error:
                self._last_esi_location_error = message
                self._log_message(f"ESI current-system sync unavailable: {message}")
            return False

        system_id = _positive_int(system.get("system_id"))
        system_name = str(
            system.get("system_name") or system.get("name") or ""
        ).strip()
        if system_id is None and not system_name:
            return False

        previous = (self._intel_system_id, self._intel_system)
        self._intel_system_id = system_id
        if system_name:
            self._intel_system = system_name
        self._intel_system_source = "esi"
        self._last_esi_location_error = ""

        current = (self._intel_system_id, self._intel_system)
        if current != previous:
            label = self._intel_system
            if self._intel_system_id is not None:
                label = f"{label} ({self._intel_system_id})"
            self._log_message(f"Current system from ESI: {label}")
        return True

    def _on_alert_closed(self) -> None:
        """Called when the alert dialog is dismissed."""
        self._alert_visible = False
        if self._alert_queue:
            next_threats = self._alert_queue.pop(0)
            self._on_threat(next_threats)

    def _log_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{timestamp}] {message}")

    def _update_scan_count(self, count: int) -> None:
        _ = count

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("EVE Sentry")
        self._tray.activated.connect(self._on_tray_activated)

        menu = self._tray.contextMenu()
        if menu is None:
            from PyQt6.QtWidgets import QMenu

            menu = QMenu()
            self._tray.setContextMenu(menu)

        show_action = QAction("Show")
        show_action.triggered.connect(self.show)
        menu.addAction(show_action)

        quit_action = QAction("Quit")
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()

    def closeEvent(self, event):
        """Minimize to tray instead of closing."""
        event.ignore()
        self.hide()

    def _quit_app(self):
        self._stop_monitor()
        self._tray.hide()
        QApplication.quit()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return max(minimum, value)


def _positive_int(value) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
