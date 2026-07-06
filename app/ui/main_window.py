"""Main application window."""

import logging
import os
import time
from datetime import datetime

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
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

from app.channel_client import process_once
from app.channels.log_watcher import ChatLogWatcher
from app.engine.capturer import Capturer
from app.core.heartbeat import (
    build_detector_heartbeat_details,
    heartbeat_now_iso,
    resolve_runtime_identity,
)
from app.engine.detector import Detector
from app.engine.ocr import OCREngine
from app.engine.worker import MonitorWorker
from app.intel_client import IntelApiClient, IntelApiError
from app.models.region_prefs import RegionPreferences
from app.models.whitelist import Whitelist
from app.ui.region_selector import RegionSelector
from app.ui.settings import SettingsPanel
from app.ui.theme import APP_QSS, monitor_button_style, status_card_style

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(860, 560)
        self.setStyleSheet(APP_QSS)

        self._whitelist = Whitelist("whitelist.json")
        self._region_prefs = RegionPreferences("region_prefs.json")
        self._capturer = Capturer()
        self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
        self._detector = Detector(self._whitelist, cooldown_seconds=60.0)
        self._worker: MonitorWorker | None = None
        self._workers: dict[str, MonitorWorker] = {}
        self._worker_contexts: dict[str, dict] = {}
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
        self._heartbeat_interval = _env_float(
            "EVE_SENTRY_HEARTBEAT_INTERVAL",
            default=15.0,
            minimum=5.0,
        )
        self._heartbeat_client_id = f"detector-client:{os.getpid()}"
        self._heartbeat_runtime = resolve_runtime_identity()
        self._heartbeat_last_action = "startup"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = ""
        self._last_heartbeat_error = ""
        self._intel_client = self._create_intel_client()
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(int(self._heartbeat_interval * 1000))
        self._heartbeat_timer.timeout.connect(self._publish_heartbeat)
        self._channel_timer = QTimer(self)
        self._channel_timer.setInterval(5000)
        self._channel_timer.timeout.connect(self._poll_channel_monitor)
        self._channel_watcher: ChatLogWatcher | None = None
        self._channel_names: list[str] = []
        self._channel_state_path = os.environ.get(
            "EVE_SENTRY_CHANNEL_STATE",
            "channel_offsets.json",
        )
        self._channel_last_action = ""
        self._channel_last_error = ""
        self._channel_last_success_at = ""

        self._popup_alerts_enabled = False
        self._alert_visible = False
        self._alert_queue: list[list[str]] = []
        self._manual_region: dict | None = None
        self._detected_region: dict | None = None
        self._status_cards: dict[str, tuple[QFrame, QLabel, QLabel]] = {}

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._settings = SettingsPanel(self._whitelist)
        self._settings.setFixedWidth(240)
        root.addWidget(self._settings)

        right = QVBoxLayout()
        right.setSpacing(6)

        self._monitor_btn = QPushButton("Start Monitor")
        self._monitor_btn.setMinimumHeight(40)
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        self._monitor_btn.setCheckable(True)
        self._monitor_btn.clicked.connect(self._toggle_monitor)
        right.addWidget(self._monitor_btn)

        self._window_combo = QComboBox()
        self._window_combo.currentIndexChanged.connect(self._on_window_selected)
        right.addWidget(self._window_combo)

        self._window_label = QLabel("Window: not detected")
        right.addWidget(self._window_label)

        status_grid = QGridLayout()
        status_grid.setSpacing(6)
        for index, key in enumerate(
            ["server", "esi", "ocr", "channel", "window", "region"]
        ):
            card = self._make_status_card(key)
            status_grid.addWidget(card, index // 3, index % 3)
        right.addLayout(status_grid)

        right.addWidget(QLabel("运行日志"))

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace;")
        right.addWidget(self._log)

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self._log.clear)
        btn_row.addWidget(clear_btn)

        select_btn = QPushButton("选择成员列表区域")
        select_btn.clicked.connect(self._select_region)
        btn_row.addWidget(select_btn)

        btn_row.addStretch()
        right.addLayout(btn_row)

        root.addLayout(right, 1)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status_label = QLabel("待机")
        self._status.addWidget(self._status_label)

        self._setup_tray()
        self._detect_window()
        if self._intel_client is not None:
            self._heartbeat_timer.start()
            self._publish_heartbeat()
        self._refresh_status_cards()

    def _make_status_card(self, key: str) -> QFrame:
        """Build a compact status card for the desktop HUD."""
        titles = {
            "server": "服务端",
            "esi": "ESI 星系",
            "ocr": "OCR 上报",
            "channel": "频道日志",
            "window": "EVE 窗口",
            "region": "监控区域",
        }
        frame = QFrame()
        frame.setObjectName(f"status-card-{key}")
        frame.setMinimumHeight(58)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(2)
        title = QLabel(titles[key])
        title.setStyleSheet("color: #79c6dc; font-size: 11px; font-weight: 600;")
        value = QLabel("未就绪")
        value.setStyleSheet("color: #f2fbff; font-size: 13px; font-weight: 700;")
        value.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(value)
        self._status_cards[key] = (frame, title, value)
        self._set_status_card(key, "未就绪", "idle")
        return frame

    def _set_status_card(self, key: str, value: str, tone: str = "idle") -> None:
        """Update a desktop HUD status card if it exists."""
        try:
            cards = self.__dict__.get("_status_cards", {})
        except RuntimeError:
            return
        card = cards.get(key)
        if card is None:
            return
        frame, _title, value_label = card
        frame.setStyleSheet(status_card_style(tone))
        value_label.setText(value)

    def _refresh_status_cards(self) -> None:
        """Refresh passive status cards from current runtime state."""
        try:
            cards = self.__dict__.get("_status_cards")
        except RuntimeError:
            return
        if not cards:
            return

        intel_client = getattr(self, "_intel_client", None)
        last_heartbeat_error = getattr(self, "_last_heartbeat_error", "")
        if intel_client is None:
            self._set_status_card("server", "未配置", "warn")
        elif last_heartbeat_error:
            self._set_status_card("server", "连接异常", "danger")
        else:
            self._set_status_card("server", "已配置", "ok")

        intel_system = getattr(self, "_intel_system", "") or "Unknown"
        intel_system_id = getattr(self, "_intel_system_id", None)
        intel_system_source = getattr(self, "_intel_system_source", "default")
        esi_label = intel_system
        if intel_system_id is not None:
            esi_label = f"{esi_label} ({intel_system_id})"
        self._set_status_card(
            "esi",
            esi_label,
            "ok" if intel_system_source == "esi" else "warn",
        )

        monitoring = self._is_monitoring()
        worker_count = len(self._running_workers())
        self._set_status_card(
            "ocr",
            f"{worker_count} 窗口监控中" if monitoring else "待启动",
            "active" if monitoring else "idle",
        )

        channel_watcher = getattr(self, "_channel_watcher", None)
        channel_names = list(getattr(self, "_channel_names", []))
        settings = getattr(self, "_settings", None)
        configured_channels = settings.get_channel_names() if settings else []
        if channel_watcher is not None:
            self._set_status_card("channel", f"{len(channel_names)} 个频道", "active")
        elif configured_channels:
            self._set_status_card("channel", "已配置, 未启动", "warn")
        else:
            self._set_status_card("channel", "未选择", "idle")

        if monitoring:
            self._set_status_card("window", f"{worker_count} 个窗口", "active")
        else:
            window_combo = getattr(self, "_window_combo", None)
            window_title = window_combo.currentText().strip() if window_combo else ""
            self._set_status_card(
                "window",
                window_title or "未检测到",
                "ok" if window_title else "warn",
            )

        contexts = getattr(self, "_worker_contexts", {})
        if monitoring and contexts:
            self._set_status_card("region", f"{len(contexts)} 个区域", "active")
        else:
            region = getattr(self, "_manual_region", None) or getattr(self, "_detected_region", None)
            if region:
                self._set_status_card("region", f"{region['w']}x{region['h']}", "ok")
            else:
                self._set_status_card("region", "未配置", "warn")

    def _is_monitoring(self) -> bool:
        """Return whether any detector worker is currently running."""
        return bool(self._running_workers())

    def _running_workers(self) -> list[MonitorWorker]:
        """Return active workers, supporting both legacy and multi-window state."""
        workers = [
            worker
            for worker in getattr(self, "_workers", {}).values()
            if worker is not None and worker.isRunning()
        ]
        legacy_worker = getattr(self, "_worker", None)
        if legacy_worker is not None and legacy_worker.isRunning():
            if legacy_worker not in workers:
                workers.append(legacy_worker)
        return workers

    def _window_monitor_key(self, window: dict) -> str:
        """Return the key used for one monitored EVE window."""
        title = str(window.get("title") or "").strip()
        if title:
            return title.casefold()
        return f"hwnd:{window.get('hwnd', '')}"

    def _window_client_id(self, window: dict) -> str:
        """Return a unique OCR client id for one EVE window."""
        raw = self._window_monitor_key(window)
        slug = "".join(
            char if ("a" <= char <= "z" or "0" <= char <= "9") else "-"
            for char in raw.lower()
        ).strip("-")
        slug = "-".join(part for part in slug.split("-") if part)
        return f"{self._heartbeat_client_id}:{slug or 'window'}"

    def _build_monitor_targets(self) -> list[dict]:
        """Build monitor targets for every currently detected EVE window."""
        keyword = self._settings.get_keyword()
        windows = self._capturer.list_eve_windows(keyword)
        if not windows:
            current = self._current_window_info()
            windows = [current] if current is not None else []

        targets: list[dict] = []
        for window in windows:
            if window is None:
                continue
            region = self._region_prefs.resolve_region(window)
            if region is None:
                region = self._capturer.get_member_list_region(window)
            key = self._window_monitor_key(window)
            targets.append(
                {
                    "key": key,
                    "client_id": self._window_client_id(window),
                    "window": dict(window),
                    "window_title": str(window.get("title") or key),
                    "region": dict(region),
                }
            )
        return targets

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
        self._refresh_status_cards()

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
        self._manual_region = None

        info = self._current_window_info()
        title = self._window_combo.currentText()
        if info is None:
            self._detected_region = None
            self._window_label.setText("Window: stale selection, re-detect needed")
            self._refresh_status_cards()
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
        self._refresh_status_cards()

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
        self._selector = RegionSelector(
            info["x"], info["y"], info["w"], info["h"], title=info["title"]
        )
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
        self._refresh_status_cards()
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
        targets = self._build_monitor_targets()
        if not targets:
            self._detect_window()
            targets = self._build_monitor_targets()

        if not targets:
            QMessageBox.critical(self, "Error", "No EVE window is available.")
            self._monitor_btn.setChecked(False)
            return

        self._refresh_intel_location(force=True)

        if not self._stop_monitor_workers(timeout_ms=5000):
            self._monitor_btn.setChecked(False)
            QMessageBox.critical(
                self,
                "Error",
                "Failed to stop the previous monitor thread.",
            )
            return

        self._workers = {}
        self._worker_contexts = {}
        interval = self._settings.get_interval()
        for target in targets:
            worker = MonitorWorker(
                Capturer(),
                OCREngine(lang="en", confidence_threshold=0.7),
                Detector(self._whitelist, cooldown_seconds=60.0),
            )
            window = target["window"]
            region = target["region"]
            worker.set_window(window)
            worker.set_region(region["x"], region["y"], region["w"], region["h"])
            worker.set_interval(interval)
            worker.threat_detected.connect(
                lambda threats, context=target: self._on_threat_detected(
                    threats, context=context
                )
            )
            worker.ocr_snapshot.connect(
                lambda names, context=target: self._publish_ocr_snapshot(
                    names, context=context
                )
            )
            worker.status_update.connect(
                lambda message, context=target: self._log_message(
                    f"{context['window_title']}: {message}"
                )
            )
            worker.scan_complete.connect(self._update_scan_count)
            self._workers[target["key"]] = worker
            self._worker_contexts[target["key"]] = target

        self._worker = next(iter(self._workers.values()), None)
        for worker in self._workers.values():
            worker.start()
        self._start_channel_monitor()

        self._monitor_btn.setText("Stop Monitor")
        self._monitor_btn.setStyleSheet(monitor_button_style(active=True))
        self._status_label.setText("Running")
        self._status_label.setStyleSheet("color: #37d6b0; font-weight: bold;")
        self._log_message(f"Monitor started for {len(self._workers)} EVE window(s)")
        self._heartbeat_last_action = f"monitor_started:{len(self._workers)}"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._publish_heartbeat()
        self._refresh_status_cards()

    def _disconnect_worker_signals(self, worker: MonitorWorker | None = None) -> None:
        """Safely disconnect all signals from the current worker."""
        worker = worker or self._worker
        if worker is None:
            return
        try:
            worker.threat_detected.disconnect()
        except TypeError:
            pass
        try:
            worker.ocr_snapshot.disconnect()
        except TypeError:
            pass
        try:
            worker.status_update.disconnect()
        except TypeError:
            pass
        try:
            worker.scan_complete.disconnect()
        except TypeError:
            pass

    def _stop_monitor(self) -> None:
        self._stop_monitor_workers(timeout_ms=3000)
        self._stop_channel_monitor()

        self._monitor_btn.setText("Start Monitor")
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        self._status_label.setText("Stopped")
        self._status_label.setStyleSheet("color: #888;")
        self._log_message("Monitor stopped")
        self._heartbeat_last_action = "monitor_stopped"
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._publish_heartbeat()
        self._refresh_status_cards()

    def _stop_monitor_workers(self, timeout_ms: int) -> bool:
        """Stop all detector workers and return whether they exited cleanly."""
        workers = list(getattr(self, "_workers", {}).values())
        legacy_worker = getattr(self, "_worker", None)
        if legacy_worker is not None and legacy_worker not in workers:
            workers.append(legacy_worker)
        if not workers:
            return True

        failed = False
        running_workers = [worker for worker in workers if worker.isRunning()]
        if running_workers:
            self._log_message(f"Stopping {len(running_workers)} monitor thread(s)...")
        for worker in workers:
            worker.stop()
        for worker in workers:
            if worker.isRunning() and not worker.wait(timeout_ms):
                failed = True
                logger.warning("Worker thread did not stop within %s ms timeout", timeout_ms)
            self._disconnect_worker_signals(worker)

        if failed:
            self._capturer = Capturer()
            self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
            self._detector = Detector(self._whitelist, cooldown_seconds=60.0)
        self._workers = {}
        self._worker_contexts = {}
        self._worker = None
        return not failed

    def _on_threat(self, threats: list[str]) -> None:
        """Show non-blocking alert dialog when threats are detected."""
        _ = threats

    def _on_threat_detected(
        self,
        threats: list[str],
        context: dict | None = None,
    ) -> None:
        """Publish detected threats to the intel server."""
        if context is None:
            self._publish_intel(threats)
        else:
            self._publish_intel(threats, context=context)

    def _start_channel_monitor(self) -> bool:
        """Start selected-channel log monitoring when configured."""
        self._stop_channel_monitor()
        self._channel_names = self._settings.get_channel_names()
        self._channel_last_action = ""
        self._channel_last_error = ""
        self._channel_last_success_at = ""
        if not self._channel_names:
            self._log_message("Channel log monitor disabled: no channel selected")
            self._refresh_status_cards()
            return False
        if self._intel_client is None:
            self._log_message("Channel log monitor disabled: server is not configured")
            self._refresh_status_cards()
            return False

        self._channel_watcher = ChatLogWatcher(
            log_dir=self._settings.get_channel_log_dir(),
            channels=self._channel_names,
            state_path=self._channel_state_path,
        )
        matched_files = self._channel_watcher.discover_files()
        self._channel_watcher.seed_to_end()
        self._channel_timer.setInterval(5000)
        self._channel_timer.start()
        joined = ", ".join(self._channel_names)
        if matched_files:
            self._log_message(
                f"Channel log monitor started: {joined} ({len(matched_files)} files)"
            )
        else:
            self._log_message(
                "Channel log monitor started with no matching files yet: "
                f"{joined}. Use full channel names or explicit * / ? wildcards."
            )
        self._refresh_status_cards()
        return True

    def _stop_channel_monitor(self) -> None:
        self._channel_timer.stop()
        self._channel_watcher = None
        self._refresh_status_cards()

    def _poll_channel_monitor(self) -> None:
        if self._channel_watcher is None or self._intel_client is None:
            return
        diagnostics = {
            "last_action": "",
            "last_error": self._channel_last_error,
            "last_success_at": self._channel_last_success_at,
        }
        try:
            processed = process_once(
                self._channel_watcher,
                self._intel_client,
                server_parse=True,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            self._channel_last_action = "observation_error"
            self._channel_last_error = str(exc)
            self._log_message(f"Channel log upload failed: {exc}")
            self._publish_heartbeat()
            self._refresh_status_cards()
            return

        self._channel_last_action = str(
            diagnostics.get("last_action") or self._channel_last_action
        )
        self._channel_last_error = str(diagnostics.get("last_error") or "")
        self._channel_last_success_at = str(
            diagnostics.get("last_success_at") or self._channel_last_success_at
        )
        if processed:
            self._log_message(f"Channel observations uploaded: {processed}")
            self._publish_heartbeat()
        self._refresh_status_cards()

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

    def _publish_intel(
        self,
        threats: list[str],
        context: dict | None = None,
    ) -> None:
        if self._intel_client is None or not threats:
            return

        self._refresh_intel_location()
        source = "eve-sentry-detector"
        window_title = (
            str(context.get("window_title") or "").strip()
            if context
            else self._window_combo.currentText()
        )
        metadata = {"system_source": self._intel_system_source}
        if window_title:
            metadata["window_title"] = window_title
        if context:
            if context.get("key"):
                metadata["target_id"] = context["key"]
            if context.get("client_id"):
                metadata["client_id"] = context["client_id"]
        try:
            created = self._intel_client.post_observation(
                system_name=self._intel_system or "Unknown",
                system_id=self._intel_system_id,
                names=threats,
                source=source,
                source_instance=window_title,
                raw_text=", ".join(threats),
                metadata=metadata,
            )
        except IntelApiError as exc:
            self._heartbeat_last_action = "observation_error"
            self._heartbeat_last_error = str(exc)
            self._log_message(f"情报上报失败: {exc}")
            self._refresh_status_cards()
            return

        observation_id = created.get("observation", {}).get("id", "")
        suffix = f" ({observation_id[:8]})" if observation_id else ""
        self._heartbeat_last_action = f"observation:{len(threats)}"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._log_message(f"已上报情报: {len(threats)} 个目标{suffix}")
        self._refresh_status_cards()

    def _publish_ocr_snapshot(
        self,
        names: list[str],
        context: dict | None = None,
    ) -> None:
        if self._intel_client is None:
            return

        self._refresh_intel_location()
        client_id = self._heartbeat_client_id
        source_instance = self._window_combo.currentText()
        if context:
            client_id = str(context.get("client_id") or client_id)
            source_instance = str(context.get("window_title") or source_instance)
        try:
            self._intel_client.post_ocr_snapshot(
                client_id=client_id,
                source_instance=source_instance,
                system_name=self._intel_system or "Unknown",
                system_id=self._intel_system_id,
                names=names,
            )
        except IntelApiError as exc:
            self._heartbeat_last_action = "ocr_snapshot_error"
            self._heartbeat_last_error = str(exc)
            self._log_message(f"OCR snapshot upload failed: {exc}")
            self._refresh_status_cards()
            return
        self._heartbeat_last_action = f"ocr_snapshot:{len(names)}"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._refresh_status_cards()

    def _publish_heartbeat(self) -> None:
        if self._intel_client is None:
            return
        monitoring = self._is_monitoring()
        try:
            details = build_detector_heartbeat_details(
                monitoring=monitoring,
                system_name=self._intel_system,
                system_source=self._intel_system_source,
                popup_alerts=self._popup_alerts_enabled,
                window_title=self._window_combo.currentText(),
                last_action=self._heartbeat_last_action,
                last_error=self._heartbeat_last_error,
                client_version=self._heartbeat_runtime["client_version"],
                host=self._heartbeat_runtime["host"],
                last_success_at=self._heartbeat_last_success_at,
            )
            contexts = list(getattr(self, "_worker_contexts", {}).values())
            if contexts:
                details["targets"] = [
                    {
                        "client_id": context["client_id"],
                        "window_title": context["window_title"],
                        "region": context["region"],
                        "monitoring": context["key"] in getattr(self, "_workers", {}),
                    }
                    for context in contexts
                ]
                details["target_count"] = len(contexts)
            details["channel_monitoring"] = self._channel_watcher is not None
            if self._channel_names:
                details["channels"] = list(self._channel_names)
            if self._channel_last_action:
                details["channel_last_action"] = self._channel_last_action
            if self._channel_last_error:
                details["channel_last_error"] = self._channel_last_error
            if self._channel_last_success_at:
                details["channel_last_success_at"] = self._channel_last_success_at
            self._intel_client.post_heartbeat(
                client_id=self._heartbeat_client_id,
                client_type="detector_client",
                label="Detector Client",
                status="running" if monitoring else "idle",
                heartbeat_interval_seconds=self._heartbeat_interval,
                details=details,
            )
            self._last_heartbeat_error = ""
        except IntelApiError as exc:
            message = str(exc)
            if message != self._last_heartbeat_error:
                self._last_heartbeat_error = message
                self._log_message(f"Heartbeat update failed: {message}")
        self._refresh_status_cards()

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
                self._heartbeat_last_action = "esi_error"
                self._heartbeat_last_error = message
                self._log_message(f"ESI current-system sync unavailable: {message}")
                self._refresh_status_cards()
            return False

        if not system:
            message = "location did not include a solar system"
            if message != self._last_esi_location_error:
                self._last_esi_location_error = message
                self._heartbeat_last_action = "esi_error"
                self._heartbeat_last_error = message
                self._log_message(f"ESI current-system sync unavailable: {message}")
                self._refresh_status_cards()
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
        self._heartbeat_last_error = ""

        current = (self._intel_system_id, self._intel_system)
        if current != previous:
            label = self._intel_system
            if self._intel_system_id is not None:
                label = f"{label} ({self._intel_system_id})"
            self._heartbeat_last_action = "esi_sync"
            self._heartbeat_last_success_at = heartbeat_now_iso()
            self._log_message(f"Current system from ESI: {label}")
        self._refresh_status_cards()
        return True

    def _on_alert_closed(self) -> None:
        """Called when the alert dialog is dismissed."""
        self._alert_visible = False
        if self._alert_queue:
            next_threats = self._alert_queue.pop(0)
            self._publish_intel(next_threats)

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
