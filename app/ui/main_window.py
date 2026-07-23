"""Main application window."""

import logging
import os
import time
from argparse import Namespace
from concurrent.futures import Future
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
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.alert_client import (
    DEFAULT_EVENT_TIMEOUT,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_RECONNECT_MAX_DELAY,
    AlertTrayController,
    default_state_path,
)
from app.channels.local_system import find_latest_local_system
from app.engine.capturer import Capturer
from app.core.heartbeat import (
    build_detector_heartbeat_details,
    heartbeat_now_iso,
    resolve_runtime_identity,
)
from app.engine.ocr import OCREngine
from app.engine.worker import MonitorWorker
from app.intel_client import IntelApiClient, IntelApiError
from app.models.region_prefs import RegionPreferences
from app.ui.background_tasks import BackgroundTaskRunner
from app.ui.region_selector import RegionSelector
from app.ui.settings import SettingsPanel
from app.ui.theme import APP_QSS, monitor_button_style, status_card_style

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(800, 540)
        self.setStyleSheet(APP_QSS)

        self._region_prefs = RegionPreferences("region_prefs.json")
        self._capturer = Capturer()
        self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
        self._worker: MonitorWorker | None = None
        self._workers: dict[str, MonitorWorker] = {}
        self._worker_contexts: dict[str, dict] = {}
        self._settings = SettingsPanel()
        self._intel_url = self._settings.get_server_url()
        configured_system = os.environ.get("EVE_SENTRY_SYSTEM", "").strip()
        self._intel_system = configured_system or "Unknown"
        self._intel_system_id: int | None = None
        self._intel_system_source = "env" if configured_system else "default"
        self._use_local_system_log = _env_flag(
            "EVE_SENTRY_USE_LOCAL_SYSTEM_LOG",
            default=not bool(configured_system),
        )
        self._location_refresh_ttl = _env_float(
            "EVE_SENTRY_LOCAL_SYSTEM_TTL",
            default=5.0,
            minimum=1.0,
        )
        self._location_next_check = 0.0
        self._last_local_system_error = ""
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
        self._uploads_enabled = False
        self._intel_client = self._create_intel_client()
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(int(self._heartbeat_interval * 1000))
        self._heartbeat_timer.timeout.connect(self._publish_heartbeat)
        self._window_refresh_timer = QTimer(self)
        self._window_refresh_timer.setInterval(3000)
        self._window_refresh_timer.timeout.connect(self._refresh_detected_windows)
        self._network_tasks = BackgroundTaskRunner(max_workers=2, parent=self)
        self._network_tasks.completed.connect(self._on_network_task_completed)
        self._alert_controller: AlertTrayController | None = None

        self._popup_alerts_enabled = False
        self._manual_region: dict | None = None
        self._detected_region: dict | None = None
        self._status_cards: dict[str, tuple[QFrame, QLabel, QLabel]] = {}

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._settings.setFixedWidth(240)
        self._settings.scan_settings_changed.connect(self._apply_scan_settings)
        self._settings.server_url_changed.connect(self._apply_server_url)
        root.addWidget(self._settings)

        right = QVBoxLayout()
        right.setSpacing(6)

        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self._monitor_btn = QPushButton("开始监控")
        self._monitor_btn.setMinimumHeight(40)
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        self._monitor_btn.setCheckable(True)
        self._monitor_btn.clicked.connect(self._toggle_monitor)
        action_row.addWidget(self._monitor_btn, 1)

        self._alert_btn = QPushButton("开启预警")
        self._alert_btn.setMinimumHeight(40)
        self._alert_btn.setStyleSheet(monitor_button_style(active=False))
        self._alert_btn.setCheckable(True)
        self._alert_btn.clicked.connect(self._toggle_alert)
        action_row.addWidget(self._alert_btn, 1)
        right.addLayout(action_row)

        self._window_combo = QComboBox()
        self._window_combo.currentIndexChanged.connect(self._on_window_selected)
        right.addWidget(self._window_combo)

        self._window_label = QLabel("窗口：未检测")
        right.addWidget(self._window_label)

        right.addWidget(QLabel("窗口状态"))
        self._window_status_table = QTableWidget(0, 4)
        self._window_status_table.setHorizontalHeaderLabels(
            ["窗口", "区域", "状态", "最近动作"]
        )
        self._window_status_table.setMinimumHeight(96)
        self._window_status_table.verticalHeader().setVisible(False)
        self._window_status_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._window_status_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._window_status_table.setColumnWidth(0, 180)
        self._window_status_table.setColumnWidth(1, 125)
        self._window_status_table.setColumnWidth(2, 90)
        self._window_status_table.horizontalHeader().setStretchLastSection(True)
        right.addWidget(self._window_status_table)

        status_grid = QGridLayout()
        status_grid.setSpacing(6)
        for index, key in enumerate(["server", "esi", "ocr", "window", "region"]):
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
        self._window_refresh_timer.start()
        self._refresh_window_status_table()
        self._refresh_status_cards()
        if _env_flag("EVE_SENTRY_AUTO_START_MONITOR", default=False):
            QTimer.singleShot(0, self._auto_start_monitor)

    def _auto_start_monitor(self) -> None:
        """Start monitoring once the event loop is ready when explicitly requested."""
        if self._monitor_btn.isChecked():
            return
        self._monitor_btn.setChecked(True)
        self._start_monitor()

    def _make_status_card(self, key: str) -> QFrame:
        """Build a compact status card for the desktop HUD."""
        titles = {
            "server": "服务端",
            "esi": "当前星系",
            "ocr": "OCR 上报",
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
            "ok" if intel_system_source in {"esi", "env", "chatlog"} else "warn",
        )

        monitoring = self._is_monitoring()
        worker_count = len(self._running_workers())
        self._set_status_card(
            "ocr",
            f"{worker_count} 窗口监控中" if monitoring else "待启动",
            "active" if monitoring else "idle",
        )

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

    def _region_label(self, region: dict | None) -> str:
        if not region:
            return "-"
        return (
            f"{int(region.get('w', 0))}x{int(region.get('h', 0))} "
            f"@ {int(region.get('x', 0))},{int(region.get('y', 0))}"
        )

    def _refresh_window_status_table(self) -> None:
        """Refresh the per-window monitor status table."""
        try:
            table = self.__dict__.get("_window_status_table")
        except RuntimeError:
            return
        if table is None:
            return

        contexts = list(getattr(self, "_worker_contexts", {}).values())
        rows: list[dict] = []
        if contexts:
            rows = contexts
        else:
            window_combo = getattr(self, "_window_combo", None)
            title = window_combo.currentText().strip() if window_combo else ""
            region = getattr(self, "_manual_region", None) or getattr(self, "_detected_region", None)
            rows = [
                {
                    "window_title": title or "未检测到 EVE 窗口",
                    "region": region,
                    "runtime_status": "待启动" if title else "未检测",
                    "last_action": "选择窗口并点击开始监控" if title else "点击刷新或确认 EVE 已启动",
                }
            ]

        table.setRowCount(len(rows))
        for row_index, context in enumerate(rows):
            values = [
                str(context.get("window_title") or "-"),
                self._region_label(context.get("region")),
                str(context.get("runtime_status") or "待启动"),
                str(context.get("last_action") or "-"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row_index, column, item)

    def _update_window_status(
        self,
        context: dict | None,
        status: str,
        action: str = "",
        error: str = "",
    ) -> None:
        if not context:
            self._refresh_window_status_table()
            return
        context["runtime_status"] = status
        if action:
            context["last_action"] = action
        if error:
            context["last_error"] = error
            context["last_action"] = error
        elif "last_error" in context:
            context["last_error"] = ""
        context["updated_at"] = datetime.now().strftime("%H:%M:%S")
        self._refresh_window_status_table()

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
        hwnd = str(window.get("hwnd") or "").strip()
        if hwnd:
            return f"hwnd:{hwnd}:{title.casefold()}" if title else f"hwnd:{hwnd}"
        return title.casefold() if title else "window"

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
        """Build one monitor target for the selected EVE window."""
        window = self._current_window_info()
        if window is None:
            return []

        region = self._region_prefs.resolve_region(window)
        if region is None:
            region = self._capturer.get_member_list_region(window)
        key = self._window_monitor_key(window)
        window_title = str(window.get("title") or key)
        configured_system = (
            _instance_attr(self, "_intel_system", "Unknown")
            if _instance_attr(self, "_intel_system_source", "default") == "env"
            else "Unknown"
        )
        return [
            {
                "key": key,
                "client_id": self._window_client_id(window),
                "window": dict(window),
                "window_title": window_title,
                "character_name": _character_name_from_window_title(window_title),
                "source_instance": self._window_combo_label(window),
                "region": dict(region),
                "system_name": configured_system,
                "system_id": None,
                "system_source": (
                    "env" if configured_system != "Unknown" else "default"
                ),
                "_location_next_check": 0.0,
            }
        ]

    def _window_combo_label(
        self,
        window: dict,
        duplicate_index: int = 1,
        duplicate_count: int = 1,
    ) -> str:
        """Return a user-facing label for one EVE window in the selector."""
        title = str(window.get("title") or "EVE 窗口").strip()
        if duplicate_count <= 1:
            return title
        details: list[str] = [f"{title} #{duplicate_index}"]
        hwnd = window.get("hwnd")
        if hwnd not in {None, ""}:
            details.append(f"hwnd {hwnd}")
        width = int(window.get("w") or 0)
        height = int(window.get("h") or 0)
        if width > 0 and height > 0:
            details.append(f"{width}x{height}")
        return " · ".join(details)

    def _detect_window(self, windows: list[dict] | None = None) -> None:
        """Find all EVE windows and populate the window selector."""
        previous_hwnd = self._window_combo.currentData()
        if windows is None:
            keyword = self._settings.get_keyword()
            windows = self._capturer.list_eve_windows(keyword)
        self._window_signature = _window_list_signature(windows)
        self._window_combo.blockSignals(True)
        self._window_combo.clear()
        selected_index = 0
        if windows:
            title_counts: dict[str, int] = {}
            for window in windows:
                title = str(window.get("title") or "").casefold()
                title_counts[title] = title_counts.get(title, 0) + 1
            title_indexes: dict[str, int] = {}
            for index, window in enumerate(windows):
                title = str(window.get("title") or "").casefold()
                title_indexes[title] = title_indexes.get(title, 0) + 1
                self._window_combo.addItem(
                    self._window_combo_label(
                        window,
                        duplicate_index=title_indexes[title],
                        duplicate_count=title_counts[title],
                    ),
                    window["hwnd"],
                )
                if window["hwnd"] == previous_hwnd:
                    selected_index = index
        self._window_combo.blockSignals(False)

        if windows:
            self._window_combo.setCurrentIndex(selected_index)
            self._on_window_selected(selected_index)
            self._log_message(f"已发现 {len(windows)} 个 EVE 窗口")
        else:
            self._detected_region = None
            self._capturer.close()
            self._window_label.setText("窗口：未找到")
        self._refresh_status_cards()
        self._refresh_window_status_table()

    def _refresh_detected_windows(self) -> None:
        """Refresh the selector when the set of EVE windows changes."""
        keyword = self._settings.get_keyword()
        windows = self._capturer.list_eve_windows(keyword)
        signature = _window_list_signature(windows)
        if signature == _instance_attr(self, "_window_signature", ()):
            return
        self._detect_window(windows=windows)

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
        if info is None:
            self._detected_region = None
            self._window_label.setText("窗口：选择已失效，请重新检测")
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
            f"窗口：{self._window_combo.currentText()} -> 成员列表 {member['w']}x{member['h']}"
        )
        self._refresh_status_cards()
        self._refresh_window_status_table()

    def _select_region(self) -> None:
        """Show overlay on top of EVE window for drag-to-select region."""
        hwnd = self._window_combo.currentData()
        if hwnd is not None:
            self._capturer.activate_window(hwnd)

        info = self._current_window_info()
        if info is None:
            info = self._capturer.find_eve_window(keyword=self._settings.get_keyword())
        if info is None:
            QMessageBox.critical(self, "错误", "未找到 EVE 窗口。")
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
            f"正在选择区域：{info['title']} ({info['x']},{info['y']}) "
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
            key = self._window_monitor_key(window)
            worker = getattr(self, "_workers", {}).get(key)
            if worker is not None:
                worker.set_region(x, y, w, h)
                context = getattr(self, "_worker_contexts", {}).get(key)
                if context is not None:
                    context["region"] = dict(self._manual_region)
                    self._update_window_status(context, "运行中", "区域已更新")
                self._heartbeat_last_action = "region_updated"
                self._heartbeat_last_success_at = heartbeat_now_iso()
                self._publish_heartbeat()
        self._window_label.setText(f"手动区域：({x},{y}) {w}x{h}")
        self._log_message(f"已保存成员列表区域 {w}x{h} @ ({x},{y})")
        self._refresh_status_cards()
        self._refresh_window_status_table()
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

    def _toggle_alert(self, checked: bool) -> None:
        if checked:
            self._start_alert()
        else:
            self._stop_alert()

    def _monitoring_system_names(self) -> list[str]:
        """Return unique systems belonging to currently running monitor workers."""
        workers = getattr(self, "_workers", {})
        contexts = getattr(self, "_worker_contexts", {})
        names: list[str] = []
        seen: set[str] = set()
        for key, context in contexts.items():
            if key not in workers:
                continue
            system = str(context.get("system_name") or "").strip()
            normalized = system.casefold()
            if not system or normalized == "unknown" or normalized in seen:
                continue
            seen.add(normalized)
            names.append(system)
        return names

    def _start_alert(self) -> None:
        """Start the server-side warning consumer inside the monitor client."""
        if self._alert_controller is not None:
            return
        app = QApplication.instance()
        if app is None:
            self._alert_btn.setChecked(False)
            return
        args = Namespace(
            server=self._intel_url,
            state=default_state_path(),
            timeout=_env_float(
                "EVE_SENTRY_ALERT_TIMEOUT",
                default=DEFAULT_EVENT_TIMEOUT,
                minimum=1.0,
            ),
            heartbeat_interval=_env_float(
                "EVE_SENTRY_ALERT_HEARTBEAT_INTERVAL",
                default=DEFAULT_HEARTBEAT_INTERVAL,
                minimum=5.0,
            ),
            reconnect_max_delay=_env_float(
                "EVE_SENTRY_ALERT_RECONNECT_MAX_DELAY",
                default=DEFAULT_RECONNECT_MAX_DELAY,
                minimum=1.0,
            ),
            hidden=False,
        )
        try:
            controller = AlertTrayController(
                app,
                args,
                tray_enabled=False,
                notification_callback=None,
            )
            controller.show_monitoring_systems(self._monitoring_system_names())
            controller.start()
        except Exception as exc:
            logger.exception("Failed to start embedded alert client")
            self._alert_btn.setChecked(False)
            QMessageBox.critical(self, "预警启动失败", str(exc))
            return
        self._alert_controller = controller
        self._alert_btn.setText("关闭预警")
        self._alert_btn.setStyleSheet(monitor_button_style(active=True))
        self._log_message("预警已开启")

    def _stop_alert(self, *, wait_for_worker: bool = False) -> None:
        """Stop embedded warning consumers without blocking routine UI toggles."""
        controller = self._alert_controller
        self._alert_controller = None
        if controller is not None:
            if wait_for_worker:
                controller.stop(wait_for_worker=True)
            else:
                stopping = _instance_attr(
                    self,
                    "_stopping_alert_controllers",
                    set(),
                )
                stopping.add(controller)
                self._stopping_alert_controllers = stopping
                controller.stop(wait_for_worker=False)
                QTimer.singleShot(100, self._reap_stopping_alert_controllers)

        if wait_for_worker:
            stopping = _instance_attr(self, "_stopping_alert_controllers", set())
            self._stopping_alert_controllers = set()
            for stopping_controller in stopping:
                stopping_controller.stop(wait_for_worker=True)
        self._alert_btn.setText("开启预警")
        self._alert_btn.setStyleSheet(monitor_button_style(active=False))
        self._log_message("预警已关闭")

    def _reap_stopping_alert_controllers(self) -> None:
        """Release asynchronously stopped alert controllers after their workers exit."""
        stopping = _instance_attr(self, "_stopping_alert_controllers", set())
        self._stopping_alert_controllers = {
            controller for controller in stopping if controller.is_running()
        }
        if self._stopping_alert_controllers:
            QTimer.singleShot(100, self._reap_stopping_alert_controllers)

    def _show_alert_notification(self, title: str, message: str) -> None:
        self._tray.showMessage(title, message)

    def _apply_scan_settings(self) -> None:
        """Apply the current scan interval without restarting active workers."""
        interval = self._settings.get_interval()
        workers = self._running_workers()
        for worker in workers:
            worker.set_interval(interval)
        if workers:
            self._log_message(f"扫描间隔已实时更新为 {interval:g} 秒")
        self._refresh_status_cards()

    def _apply_server_url(self, server_url: str) -> None:
        """Rebuild server-backed clients after the saved URL changes."""
        server_url = str(server_url or "").strip().rstrip("/")
        if not server_url or server_url == self._intel_url:
            return

        restart_alert = self._alert_controller is not None
        self._intel_url = server_url
        self._intel_client = self._create_intel_client()
        self._last_heartbeat_error = ""
        self._heartbeat_last_error = ""
        self._log_message(f"服务端地址已更新：{server_url}")

        if restart_alert:
            self._stop_alert()
            self._start_alert()
        if self._uploads_enabled:
            self._refresh_intel_location(force=True)
            self._publish_heartbeat()
        self._refresh_status_cards()

    def _set_heartbeat_enabled(self, enabled: bool) -> None:
        timer = _instance_attr(self, "_heartbeat_timer")
        if timer is None:
            return
        if enabled:
            if not timer.isActive():
                timer.start()
            return
        timer.stop()

    def _on_network_task_completed(
        self,
        key: str,
        future: Future,
        context: object,
    ) -> None:
        """Handle one blocking network job after it leaves the GUI thread."""
        metadata = context if isinstance(context, dict) else {}
        kind = str(metadata.get("kind") or "")
        try:
            future.result()
        except Exception as exc:
            if kind == "ocr":
                self._handle_ocr_publish_error(exc, metadata)
            elif kind == "heartbeat":
                self._handle_heartbeat_publish_error(exc)
        else:
            if kind == "ocr":
                self._handle_ocr_publish_success(metadata)
            elif kind == "heartbeat":
                self._last_heartbeat_error = ""
                self._refresh_status_cards()
        finally:
            runner = _instance_attr(self, "_network_tasks")
            if runner is not None:
                runner.finish(key)

    def _handle_ocr_publish_error(self, exc: Exception, metadata: dict) -> None:
        message = str(exc)
        context = metadata.get("context")
        self._heartbeat_last_action = "ocr_snapshot_error"
        self._heartbeat_last_error = message
        self._log_message(f"OCR snapshot upload failed: {message}")
        self._update_window_status(context, "上报异常", message, error=message)
        self._refresh_status_cards()

    def _handle_ocr_publish_success(self, metadata: dict) -> None:
        names = list(metadata.get("names") or [])
        context = metadata.get("context")
        self._heartbeat_last_action = f"ocr_snapshot:{len(names)}"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._update_window_status(context, "运行中", f"OCR 名单 {len(names)}")
        self._refresh_status_cards()

    def _handle_heartbeat_publish_error(self, exc: Exception) -> None:
        message = str(exc)
        if message != self._last_heartbeat_error:
            self._last_heartbeat_error = message
            self._log_message(f"Heartbeat update failed: {message}")
        self._refresh_status_cards()

    def _start_monitor(self) -> None:
        targets = self._build_monitor_targets()
        if not targets:
            self._detect_window()
            targets = self._build_monitor_targets()

        if not targets:
            QMessageBox.critical(
                self,
                "错误",
                "当前没有可用的 EVE 窗口。",
            )
            self._monitor_btn.setChecked(False)
            return

        for target in targets:
            self._refresh_intel_location(force=True, context=target)
        primary_target = targets[0]
        self._intel_system = str(primary_target.get("system_name") or "Unknown")
        self._intel_system_id = primary_target.get("system_id")
        self._intel_system_source = str(
            primary_target.get("system_source") or "default"
        )

        if not self._stop_monitor_workers(timeout_ms=5000):
            self._monitor_btn.setChecked(False)
            QMessageBox.critical(
                self,
                "错误",
                "无法停止上一轮监控线程。",
            )
            return

        self._workers = {}
        self._worker_contexts = {}
        interval = self._settings.get_interval()
        for target in targets:
            worker = MonitorWorker(
                Capturer(),
                OCREngine(lang="en", confidence_threshold=0.7),
            )
            window = target["window"]
            region = target["region"]
            worker.set_window(window)
            worker.set_region(region["x"], region["y"], region["w"], region["h"])
            worker.set_interval(interval)
            worker.ocr_snapshot.connect(
                lambda names, hostile_icon_count, context=target: self._publish_ocr_snapshot(
                    names,
                    context=context,
                    hostile_icon_count=hostile_icon_count,
                )
            )
            worker.hostile_detected.connect(
                lambda count, context=target: self._on_hostile_icon_detected(
                    count,
                    context,
                )
            )
            worker.status_update.connect(
                lambda message, context=target: self._on_worker_status_update(
                    message,
                    context,
                )
            )
            worker.scan_complete.connect(self._update_scan_count)
            target["runtime_status"] = "准备中"
            target["last_action"] = "等待 OCR 初始化"
            self._workers[target["key"]] = worker
            self._worker_contexts[target["key"]] = target

        self._worker = next(iter(self._workers.values()), None)
        for worker in self._workers.values():
            worker.start()
        for target in self._worker_contexts.values():
            self._update_window_status(target, "运行中", "监控线程已启动")
        self._uploads_enabled = True
        self._set_heartbeat_enabled(True)
        controller = _instance_attr(self, "_alert_controller")
        if controller is not None:
            controller.show_monitoring_systems(self._monitoring_system_names())

        self._monitor_btn.setText("停止监控")
        self._monitor_btn.setStyleSheet(monitor_button_style(active=True))
        self._status_label.setText("监控中")
        self._status_label.setStyleSheet("color: #37d6b0; font-weight: bold;")
        self._log_message(f"已启动 {len(self._workers)} 个 EVE 窗口监控")
        self._heartbeat_last_action = f"monitor_started:{len(self._workers)}"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._publish_heartbeat()
        self._refresh_status_cards()
        self._refresh_window_status_table()

    def _on_worker_status_update(self, message: str, context: dict) -> None:
        """Record one worker status update in log and the per-window table."""
        text = str(message or "").strip()
        if text.startswith("名单识别:"):
            return
        routine_update = text.startswith("名单已上报:") or text == "未识别到名单"
        now = time.monotonic()
        last_routine_log_at = float(context.get("_last_routine_log_at") or 0.0)
        if not routine_update or now - last_routine_log_at >= 15.0:
            self._log_message(f"{context['window_title']}: {text}")
            if routine_update:
                context["_last_routine_log_at"] = now
        lowered = text.casefold()
        status = "运行中"
        if "error" in lowered or "失败" in text or "异常" in text:
            status = "异常"
        elif "ocr" in lowered or "scan" in lowered or "扫描" in text:
            status = "扫描中"
        self._update_window_status(context, status, text)

    def _on_hostile_icon_detected(self, count: int, context: dict) -> None:
        """Update the local system alert as soon as its red-icon count changes."""
        controller = _instance_attr(self, "_alert_controller")
        if controller is None:
            return
        window_title = str(context.get("window_title") or "EVE").strip() or "EVE"
        system_name = str(context.get("system_name") or "Unknown").strip()
        hostile_count = max(0, int(count))
        if hostile_count == 0:
            message = f"✅ {system_name} 清空"
            self._log_message(f"{window_title}: {message}")
            self._update_window_status(context, "监控中", message)
            controller.update_local_hostile_count(system_name, 0)
            return
        message = f"❗ {system_name} 来敌"
        self._log_message(f"{window_title}: {message}")
        self._update_window_status(context, "敌对告警", message)
        controller.update_local_hostile_count(system_name, hostile_count)

    def _disconnect_worker_signals(self, worker: MonitorWorker | None = None) -> None:
        """Safely disconnect all signals from the current worker."""
        worker = worker or self._worker
        if worker is None:
            return
        try:
            worker.ocr_snapshot.disconnect()
        except TypeError:
            pass
        try:
            worker.hostile_detected.disconnect()
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

    def _stop_monitor(self, *, wait_for_workers: bool = False) -> None:
        self._uploads_enabled = False
        self._set_heartbeat_enabled(False)
        network_tasks = _instance_attr(self, "_network_tasks")
        if network_tasks is not None:
            network_tasks.cancel_latest()
        self._stop_monitor_workers(
            timeout_ms=None if wait_for_workers else 3000,
        )
        self._monitor_btn.setText("开始监控")
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        self._status_label.setText("已停止")
        self._status_label.setStyleSheet("color: #888;")
        self._log_message("监控已停止")
        self._heartbeat_last_action = "monitor_stopped"
        self._refresh_status_cards()

    def _stop_monitor_workers(self, timeout_ms: int | None) -> bool:
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
            self._log_message(f"正在停止 {len(running_workers)} 个监控线程...")
        for worker in workers:
            worker.stop()
        for worker in workers:
            if worker.isRunning():
                stopped = worker.wait() if timeout_ms is None else worker.wait(timeout_ms)
                if not stopped:
                    failed = True
                    logger.warning(
                        "Worker thread did not stop within %s ms timeout",
                        timeout_ms,
                    )
            self._disconnect_worker_signals(worker)

        if failed:
            self._capturer = Capturer()
            self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
        self._workers = {}
        self._worker_contexts = {}
        self._worker = None
        self._refresh_window_status_table()
        return not failed

    def _create_intel_client(self) -> IntelApiClient | None:
        enabled = (
            os.environ.get("EVE_SENTRY_PUBLISH_INTEL", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if not enabled or not self._intel_url:
            return None
        timeout_raw = os.environ.get("EVE_SENTRY_INTEL_TIMEOUT", "10.0")
        try:
            timeout = max(0.1, float(timeout_raw))
        except ValueError:
            timeout = 10.0
        return IntelApiClient(self._intel_url, timeout=timeout)

    def _publish_ocr_snapshot(
        self,
        names: list[str],
        context: dict | None = None,
        hostile_icon_count: int = 0,
    ) -> None:
        if (
            self._intel_client is None
            or not _instance_attr(self, "_uploads_enabled", True)
        ):
            return

        client_id = self._heartbeat_client_id
        source_instance = self._window_combo.currentText()
        if context:
            client_id = str(context.get("client_id") or client_id)
            source_instance = str(
                context.get("source_instance")
                or context.get("window_title")
                or source_instance
            )
        if context is None:
            self._refresh_intel_location()
        else:
            self._refresh_intel_location(context=context)
        if context is None:
            system_name = self._intel_system
            system_id = self._intel_system_id
        else:
            system_name = str(context.get("system_name") or "Unknown")
            system_id = context.get("system_id")
        payload = {
            "client_id": client_id,
            "source_instance": source_instance,
            "system_name": system_name or "Unknown",
            "system_id": system_id,
            "names": list(names),
        }
        if hostile_icon_count > 0:
            payload["hostile_icon_count"] = int(hostile_icon_count)
        runner = _instance_attr(self, "_network_tasks")
        if runner is not None:
            client = self._intel_client
            runner.submit_latest(
                f"ocr:{client_id}",
                lambda: client.post_ocr_snapshot(**payload),
                {
                    "kind": "ocr",
                    "context": context,
                    "names": list(names),
                },
            )
            return
        try:
            self._intel_client.post_ocr_snapshot(**payload)
        except IntelApiError as exc:
            self._heartbeat_last_action = "ocr_snapshot_error"
            self._heartbeat_last_error = str(exc)
            self._log_message(f"OCR snapshot upload failed: {exc}")
            self._update_window_status(context, "上报异常", str(exc), error=str(exc))
            self._refresh_status_cards()
            return
        self._heartbeat_last_action = f"ocr_snapshot:{len(names)}"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
        self._update_window_status(context, "运行中", f"OCR 名单 {len(names)}")
        self._refresh_status_cards()

    def _publish_heartbeat(self) -> None:
        if (
            self._intel_client is None
            or not _instance_attr(self, "_uploads_enabled", True)
        ):
            return
        monitoring = self._is_monitoring()
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
                    "source_instance": context.get(
                        "source_instance",
                        context["window_title"],
                    ),
                    "character_name": context.get("character_name", ""),
                    "system_name": context.get("system_name", "Unknown"),
                    "system_id": context.get("system_id"),
                    "system_source": context.get("system_source", "default"),
                    "region": context["region"],
                    "monitoring": context["key"] in getattr(self, "_workers", {}),
                }
                for context in contexts
            ]
            details["target_count"] = len(contexts)
        payload = {
            "client_id": self._heartbeat_client_id,
            "client_type": "detector_client",
            "label": "Detector Client",
            "status": "running" if monitoring else "idle",
            "heartbeat_interval_seconds": self._heartbeat_interval,
            "details": details,
        }
        runner = _instance_attr(self, "_network_tasks")
        if runner is not None:
            client = self._intel_client
            runner.submit_latest(
                "heartbeat",
                lambda: client.post_heartbeat(**payload),
                {"kind": "heartbeat"},
            )
            return
        try:
            self._intel_client.post_heartbeat(**payload)
            self._last_heartbeat_error = ""
        except IntelApiError as exc:
            message = str(exc)
            if message != self._last_heartbeat_error:
                self._last_heartbeat_error = message
                self._log_message(f"Heartbeat update failed: {message}")
        self._refresh_status_cards()

    def _refresh_intel_location(
        self,
        force: bool = False,
        context: dict | None = None,
    ) -> bool:
        now = time.monotonic()
        if context is None:
            system_source = self._intel_system_source
            system_name = self._intel_system
            next_check = self._location_next_check
        else:
            system_source = str(context.get("system_source") or "default")
            system_name = str(context.get("system_name") or "Unknown")
            next_check = float(context.get("_location_next_check") or 0.0)
        has_cached_location = bool(
            system_source in {"env", "chatlog"}
            and system_name
            and system_name != "Unknown"
        )
        if not force and now < next_check:
            return has_cached_location
        next_check = now + self._location_refresh_ttl
        if context is None:
            self._location_next_check = next_check
        else:
            context["_location_next_check"] = next_check

        return (
            self._refresh_local_system_from_chatlog(context=context)
            or has_cached_location
        )

    def _refresh_local_system_from_chatlog(self, context: dict | None = None) -> bool:
        if not getattr(self, "_use_local_system_log", False):
            return False
        settings = getattr(self, "_settings", None)
        if settings is None:
            return False

        character_name = ""
        if context is not None:
            character_name = str(context.get("character_name") or "").strip()
            if not character_name:
                return False
        try:
            detection = find_latest_local_system(
                settings.get_channel_log_dir(),
                character_name=character_name,
            )
        except Exception as exc:
            message = str(exc)
            if message != self._last_local_system_error:
                self._last_local_system_error = message
                self._log_message(f"Local chatlog system sync unavailable: {message}")
            return False
        if detection is None:
            return False

        if context is None:
            previous = self._intel_system
            self._intel_system = detection.system_name
            self._intel_system_id = None
            self._intel_system_source = "chatlog"
        else:
            previous = str(context.get("system_name") or "Unknown")
            context["system_name"] = detection.system_name
            context["system_id"] = None
            context["system_source"] = "chatlog"
        self._last_local_system_error = ""
        self._heartbeat_last_error = ""
        if detection.system_name != previous:
            self._heartbeat_last_action = "local_system_sync"
            self._heartbeat_last_success_at = heartbeat_now_iso()
            character_label = f" for {character_name}" if character_name else ""
            self._log_message(
                "Current system from local chatlog"
                f"{character_label}: {detection.system_name}"
            )
        self._refresh_status_cards()
        return True

    def _log_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{timestamp}] {message}")

    def _update_scan_count(self, count: int) -> None:
        _ = count

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self._tray.setIcon(icon)
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
        """Stop background monitoring before closing the application."""
        self._quit_app()
        event.accept()

    def _quit_app(self):
        self._stop_alert(wait_for_worker=True)
        self._stop_monitor(wait_for_workers=True)
        network_tasks = _instance_attr(self, "_network_tasks")
        if network_tasks is not None:
            network_tasks.shutdown()
        self._tray.hide()
        QApplication.quit()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _instance_attr(instance, name: str, default=None):
    """Read attributes safely from normal and test-constructed Qt objects."""
    try:
        return instance.__dict__.get(name, default)
    except RuntimeError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return max(minimum, value)


def _character_name_from_window_title(title: str) -> str:
    """Return the EVE character name embedded in a game window title."""
    prefix, separator, character_name = str(title or "").partition(" - ")
    if not separator or prefix.strip().casefold() != "eve":
        return ""
    return character_name.strip()


def _window_list_signature(windows: list[dict]) -> tuple[tuple, ...]:
    """Return stable identity and geometry data for detected EVE windows."""
    return tuple(
        (
            window.get("hwnd"),
            str(window.get("title") or ""),
            int(window.get("x") or 0),
            int(window.get("y") or 0),
            int(window.get("w") or 0),
            int(window.get("h") or 0),
        )
        for window in windows
    )
