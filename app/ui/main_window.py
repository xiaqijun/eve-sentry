"""Main application window."""

import logging
import os
import time
from argparse import Namespace
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHeaderView,
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
    QToolButton,
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
from app.channels.identity_logs import EveIdentityLogScanner
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
from app.updater import ClientUpdater

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(900, 620)
        self.resize(980, 680)
        self.setStyleSheet(APP_QSS)

        self._region_prefs = RegionPreferences("region_prefs.json")
        self._capturer = Capturer()
        self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
        self._worker: MonitorWorker | None = None
        self._workers: dict[str, MonitorWorker] = {}
        self._worker_contexts: dict[str, dict] = {}
        self._settings = SettingsPanel()
        self._updater = ClientUpdater(parent=self)
        self._updater.state_changed.connect(self._settings.set_update_state)
        self._updater.restart_requested.connect(self._quit_app)
        self._settings.update_requested.connect(self._updater.request_action)
        self._identity_scanner = EveIdentityLogScanner(
            self._settings.get_channel_log_dir(),
            self._settings.auth_state_store(),
        )
        self._identity_check_running = False
        self._api_key_validated = False
        self._identity_wants_monitor = False
        self._identity_wants_alert = False
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
        self._network_tasks.submit_once(
            "ocr_init",
            self._ocr.initialize,
            {"kind": "ocr_init"},
        )
        self._identity_timer = QTimer(self)
        self._identity_timer.setInterval(10000)
        self._identity_timer.timeout.connect(self._poll_identity_logs)
        self._identity_timer.start()
        self._alert_controller: AlertTrayController | None = None
        self._stopping_monitor_workers: set[MonitorWorker] = set()

        self._popup_alerts_enabled = False
        self._manual_region: dict | None = None
        self._detected_region: dict | None = None
        self._status_cards: dict[str, tuple[QFrame, QLabel, QLabel]] = {}

        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._settings.setFixedWidth(240)
        self._settings.scan_settings_changed.connect(self._apply_scan_settings)
        self._settings.server_url_changed.connect(self._apply_server_url)
        self._settings.api_key_changed.connect(self._apply_api_key)
        root.addWidget(self._settings)

        workspace = QWidget()
        workspace.setObjectName("workspace")
        right = QVBoxLayout(workspace)
        right.setContentsMargins(20, 16, 20, 14)
        right.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        page_title = QLabel("监控中心")
        page_title.setObjectName("pageTitle")
        header_row.addWidget(page_title)
        header_row.addStretch()

        self._monitor_btn = QPushButton("开始监控")
        self._monitor_btn.setObjectName("primaryAction")
        self._monitor_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self._monitor_btn.setMinimumSize(132, 38)
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        self._monitor_btn.setCheckable(True)
        self._monitor_btn.clicked.connect(self._toggle_monitor)
        header_row.addWidget(self._monitor_btn)

        self._alert_btn = QPushButton("开启预警")
        self._alert_btn.setObjectName("alertAction")
        self._alert_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        )
        self._alert_btn.setMinimumSize(120, 38)
        self._alert_btn.setStyleSheet(monitor_button_style(active=False))
        self._alert_btn.setCheckable(True)
        self._alert_btn.clicked.connect(self._toggle_alert)
        header_row.addWidget(self._alert_btn)
        right.addLayout(header_row)

        target_panel = QFrame()
        target_panel.setObjectName("targetBar")
        target_layout = QVBoxLayout(target_panel)
        target_layout.setContentsMargins(12, 9, 12, 9)
        target_layout.setSpacing(5)

        target_row = QHBoxLayout()
        target_row.setSpacing(8)
        target_label = QLabel("目标窗口")
        target_label.setObjectName("fieldTitle")
        target_row.addWidget(target_label)

        self._window_combo = QComboBox()
        self._window_combo.setMinimumHeight(34)
        self._window_combo.currentIndexChanged.connect(self._on_window_selected)
        target_row.addWidget(self._window_combo, 1)

        refresh_btn = QToolButton()
        refresh_btn.setObjectName("iconButton")
        refresh_btn.setFixedSize(34, 34)
        refresh_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        refresh_btn.setToolTip("刷新窗口")
        refresh_btn.clicked.connect(self._detect_window)
        target_row.addWidget(refresh_btn)

        select_btn = QPushButton("选择区域")
        select_btn.setObjectName("secondaryAction")
        select_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon)
        )
        select_btn.clicked.connect(self._select_region)
        target_row.addWidget(select_btn)
        target_layout.addLayout(target_row)

        self._window_label = QLabel("窗口：未检测")
        self._window_label.setObjectName("targetMeta")
        target_layout.addWidget(self._window_label)
        right.addWidget(target_panel)

        status_title = QLabel("运行状态")
        status_title.setObjectName("sectionTitle")
        right.addWidget(status_title)

        status_grid = QGridLayout()
        status_grid.setSpacing(7)
        status_positions = {
            "server": (0, 0, 1, 1),
            "esi": (0, 1, 1, 1),
            "ocr": (0, 2, 1, 1),
            "window": (1, 0, 1, 2),
            "region": (1, 2, 1, 1),
        }
        for key, position in status_positions.items():
            status_grid.addWidget(self._make_status_card(key), *position)
        for column in range(3):
            status_grid.setColumnStretch(column, 1)
        right.addLayout(status_grid)

        table_title = QLabel("窗口状态")
        table_title.setObjectName("sectionTitle")
        right.addWidget(table_title)
        self._window_status_table = QTableWidget(0, 4)
        self._window_status_table.setHorizontalHeaderLabels(
            ["窗口", "区域", "状态", "最近动作"]
        )
        self._window_status_table.setMinimumHeight(112)
        self._window_status_table.setMaximumHeight(148)
        self._window_status_table.verticalHeader().setVisible(False)
        self._window_status_table.verticalHeader().setDefaultSectionSize(30)
        self._window_status_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._window_status_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._window_status_table.setAlternatingRowColors(True)
        self._window_status_table.setShowGrid(False)
        table_header = self._window_status_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        right.addWidget(self._window_status_table)

        log_header = QHBoxLayout()
        log_header.setSpacing(6)
        log_title = QLabel("运行日志")
        log_title.setObjectName("sectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch()

        clear_btn = QToolButton()
        clear_btn.setObjectName("iconButton")
        clear_btn.setFixedSize(30, 30)
        clear_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton)
        )
        clear_btn.setToolTip("清空日志")
        log_header.addWidget(clear_btn)
        right.addLayout(log_header)

        self._log = QTextEdit()
        clear_btn.clicked.connect(self._log.clear)
        self._log.setReadOnly(True)
        self._log.setObjectName("runtimeLog")
        right.addWidget(self._log)

        root.addWidget(workspace, 1)

        self._status = QStatusBar()
        self._status.setObjectName("appStatusBar")
        self.setStatusBar(self._status)
        self._status_label = QLabel("待机")
        self._status.addWidget(self._status_label)

        self._setup_tray()
        self._detect_window()
        self._window_refresh_timer.start()
        self._refresh_window_status_table()
        self._refresh_status_cards()
        if self._settings.get_api_key():
            QTimer.singleShot(0, self._begin_identity_check)
        if _env_flag("EVE_SENTRY_AUTO_START_MONITOR", default=False):
            QTimer.singleShot(0, self._auto_start_monitor)
        QTimer.singleShot(1500, self._updater.check)

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
        layout.setContentsMargins(11, 7, 10, 7)
        layout.setSpacing(2)
        title = QLabel(titles[key])
        title.setObjectName("statusCardTitle")
        value = QLabel("未就绪")
        value.setObjectName("statusCardValue")
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
        self._sync_monitor_target_geometry(windows)
        signature = _window_list_signature(windows)
        if signature == _instance_attr(self, "_window_signature", ()):
            return
        self._detect_window(windows=windows)

    def _sync_monitor_target_geometry(self, windows: list[dict]) -> None:
        """Remap active capture regions when an EVE window moves or resizes."""
        current_by_hwnd = {window.get("hwnd"): window for window in windows}
        workers = _instance_attr(self, "_workers", {})
        contexts = _instance_attr(self, "_worker_contexts", {})
        for key, context in contexts.items():
            worker = workers.get(key)
            previous_window = context.get("window") or {}
            current_window = current_by_hwnd.get(previous_window.get("hwnd"))
            if worker is None or current_window is None:
                continue
            previous_geometry = _window_geometry_signature(previous_window)
            current_geometry = _window_geometry_signature(current_window)
            if previous_geometry == current_geometry:
                continue
            region = self._region_prefs.resolve_region(current_window)
            if region is None:
                region = self._capturer.get_member_list_region(current_window)
            worker.set_region(region["x"], region["y"], region["w"], region["h"])
            context["window"] = dict(current_window)
            context["region"] = dict(region)

        selected_hwnd = self._window_combo.currentData()
        selected_window = current_by_hwnd.get(selected_hwnd)
        if selected_window is not None:
            self._set_alert_anchor(selected_window)

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
        self._set_alert_anchor(info)
        self._window_label.setText(
            f"窗口：{self._window_combo.currentText()} -> 成员列表 {member['w']}x{member['h']}"
        )
        self._refresh_status_cards()
        self._refresh_window_status_table()

    def _set_alert_anchor(self, window: dict | None) -> None:
        """Keep the embedded alert overlay on the selected EVE display."""
        controller = _instance_attr(self, "_alert_controller")
        setter = getattr(controller, "set_anchor_window", None)
        if callable(setter):
            setter(window)

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

    def _start_alert(self, *, identity_checked: bool = False) -> None:
        """Start the server-side warning consumer inside the monitor client."""
        if self._alert_controller is not None:
            return
        if not identity_checked:
            self._begin_identity_check("alert")
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
            api_key=(
                _instance_attr(self, "_settings").get_api_key()
                if _instance_attr(self, "_settings") is not None
                else ""
            ),
        )
        try:
            controller = AlertTrayController(
                app,
                args,
                tray_enabled=False,
                notification_callback=None,
            )
            anchor_setter = getattr(controller, "set_anchor_window", None)
            if callable(anchor_setter):
                anchor_setter(self._current_window_info())
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
        self._api_key_validated = False
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
        settings = _instance_attr(self, "_settings")
        if settings is not None and settings.get_api_key():
            QTimer.singleShot(0, self._begin_identity_check)

    def _apply_api_key(self, _api_key: str) -> None:
        """Reset network clients and local runtime state after a key change."""
        was_monitoring = self._is_monitoring()
        was_alerting = self._alert_controller is not None
        if was_monitoring:
            self._stop_monitor()
            self._monitor_btn.setChecked(False)
        if was_alerting:
            self._stop_alert()
            self._alert_btn.setChecked(False)
        self._intel_client = self._create_intel_client()
        self._api_key_validated = False
        self._identity_wants_monitor = False
        self._identity_wants_alert = False
        self._settings.set_auth_status(
            "等待身份校验" if self._settings.get_api_key() else "未配置认证密钥"
        )
        if self._settings.get_api_key():
            QTimer.singleShot(0, self._begin_identity_check)

    def _begin_identity_check(self, action: str = "runtime") -> None:
        """Validate the API key before enabling an authenticated feature."""
        if action == "monitor":
            self._identity_wants_monitor = True
        elif action == "alert":
            self._identity_wants_alert = True
        if _instance_attr(self, "_identity_check_running", False):
            return

        api_key = self._settings.get_api_key()
        if not api_key:
            self._settings.set_auth_status("请先填写客户端认证密钥", error=True)
            if action == "monitor":
                self._monitor_btn.setChecked(False)
            elif action == "alert":
                self._alert_btn.setChecked(False)
            return
        if self._intel_client is None:
            self._settings.set_auth_status("认证客户端不可用，请检查服务端地址", error=True)
            if action == "monitor":
                self._monitor_btn.setChecked(False)
            elif action == "alert":
                self._alert_btn.setChecked(False)
            return

        if _instance_attr(self, "_api_key_validated", False):
            if action == "monitor":
                self._identity_wants_monitor = False
                self._start_monitor(identity_checked=True)
            elif action == "alert":
                self._identity_wants_alert = False
                self._start_alert(identity_checked=True)
            return

        self._identity_check_running = True
        self._settings.set_auth_status("正在验证密钥")
        if action == "monitor":
            self._monitor_btn.setEnabled(False)
        elif action == "alert":
            self._alert_btn.setEnabled(False)
        client = self._intel_client
        self._network_tasks.submit_latest(
            "identity",
            lambda: self._validate_api_key(client),
            {"kind": "identity", "action": action},
        )

    def _validate_api_key(self, client: IntelApiClient) -> dict:
        user = client.validate_api_key()
        scanner = _instance_attr(self, "_identity_scanner")
        if scanner is not None:
            scanner.mark_key_validated()
        return {"user": user}

    def _scan_and_validate_identities(
        self,
        client: IntelApiClient,
        api_key: str,
    ) -> dict:
        scan = self._identity_scanner.scan(api_key)
        pending_names = list(scan.pending_characters)
        if pending_names:
            identity = client.verify_eve_characters(pending_names)
            self._identity_scanner.mark_verified(pending_names)
        else:
            identity = {"verified": scan.identity_verified, "permanent": True}
        state = self._settings.auth_state_store().load()
        return {
            "identity": identity,
            "characters": list(state.get("characters") or []),
            "processed_count": scan.processed_count,
            "pending_files": list(scan.pending_files),
        }

    def _poll_identity_logs(self) -> None:
        """Silently discover and upload Listener identities in the background."""
        scanner = _instance_attr(self, "_identity_scanner")
        client = _instance_attr(self, "_intel_client")
        if (
            scanner is None
            or client is None
            or not self._settings.get_api_key()
            or _instance_attr(self, "_listener_scan_running", False)
        ):
            return
        scanner.log_dir = Path(self._settings.get_channel_log_dir())
        self._listener_scan_running = True
        api_key = self._settings.get_api_key()
        self._network_tasks.submit_latest(
            "listener",
            lambda: self._scan_and_validate_identities(client, api_key),
            {"kind": "listener"},
        )

    def _handle_identity_check_success(self, result: object, metadata: dict) -> None:
        self._identity_check_running = False
        self._api_key_validated = True
        self._monitor_btn.setEnabled(True)
        self._alert_btn.setEnabled(True)
        self._settings.set_auth_status("认证成功")

        action = str(metadata.get("action") or "runtime")
        resume_monitor = self._identity_wants_monitor or action == "monitor"
        resume_alert = self._identity_wants_alert or action == "alert"
        self._identity_wants_monitor = False
        self._identity_wants_alert = False
        if resume_monitor and not self._is_monitoring():
            self._monitor_btn.setChecked(True)
            self._start_monitor(identity_checked=True)
        if resume_alert and self._alert_controller is None:
            self._alert_btn.setChecked(True)
            self._start_alert(identity_checked=True)

    def _handle_identity_check_error(self, exc: Exception, metadata: dict) -> None:
        self._identity_check_running = False
        self._monitor_btn.setEnabled(True)
        self._alert_btn.setEnabled(True)
        message = str(exc)
        action = str(metadata.get("action") or "runtime")
        if _is_auth_rejection(message):
            self._disable_authenticated_features(message)
            return
        self._identity_wants_monitor = False
        self._identity_wants_alert = False
        if action == "monitor" and not self._is_monitoring():
            self._monitor_btn.setChecked(False)
        if action == "alert" and self._alert_controller is None:
            self._alert_btn.setChecked(False)
        self._settings.set_auth_status(message, error=True)
        self._log_message(f"客户端密钥验证失败：{message}")

    def _handle_listener_scan_success(self) -> None:
        self._listener_scan_running = False

    def _handle_listener_scan_error(self, exc: Exception) -> None:
        self._listener_scan_running = False
        message = str(exc)
        if _is_auth_rejection(message):
            self._disable_authenticated_features(message)
            return
        logger.warning("Background Listener validation failed: %s", message)

    def _disable_authenticated_features(self, message: str) -> None:
        self._api_key_validated = False
        self._identity_wants_monitor = False
        self._identity_wants_alert = False
        if self._is_monitoring():
            self._stop_monitor()
        if self._alert_controller is not None:
            self._stop_alert()
        self._monitor_btn.setChecked(False)
        self._alert_btn.setChecked(False)
        self._settings.set_auth_status(message, error=True)
        self._log_message(f"客户端认证失效：{message}")

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
            result = future.result()
        except Exception as exc:
            if kind == "ocr":
                self._handle_ocr_publish_error(exc, metadata)
            elif kind == "heartbeat":
                self._handle_heartbeat_publish_error(exc)
            elif kind == "identity":
                self._handle_identity_check_error(exc, metadata)
            elif kind == "listener":
                self._handle_listener_scan_error(exc)
        else:
            if kind == "ocr":
                self._handle_ocr_publish_success(metadata)
            elif kind == "heartbeat":
                self._last_heartbeat_error = ""
                self._refresh_status_cards()
            elif kind == "identity":
                self._handle_identity_check_success(result, metadata)
            elif kind == "listener":
                self._handle_listener_scan_success()
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
        if _is_auth_rejection(message):
            self._disable_authenticated_features(message)
            return
        if message != self._last_heartbeat_error:
            self._last_heartbeat_error = message
            self._log_message(f"Heartbeat update failed: {message}")
        self._refresh_status_cards()

    def _start_monitor(self, *, identity_checked: bool = False) -> None:
        if not identity_checked:
            self._begin_identity_check("monitor")
            return
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
        for index, target in enumerate(targets):
            ocr_engine = (
                self._ocr
                if index == 0
                else OCREngine(lang="en", confidence_threshold=0.7)
            )
            worker = MonitorWorker(
                Capturer(),
                ocr_engine,
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
        monitoring_systems = self._monitoring_system_names()
        controller = _instance_attr(self, "_alert_controller")
        forget_systems = getattr(controller, "forget_local_monitoring_systems", None)
        if callable(forget_systems):
            forget_systems(monitoring_systems)
        network_tasks = _instance_attr(self, "_network_tasks")
        if network_tasks is not None:
            network_tasks.cancel_latest()
        self._heartbeat_last_action = "monitor_stopped"
        if _instance_attr(self, "_uploads_enabled", False):
            self._publish_heartbeat(
                monitoring_override=False,
                task_key="heartbeat:offline",
            )
        self._uploads_enabled = False
        self._set_heartbeat_enabled(False)
        self._stop_monitor_workers(
            timeout_ms=None if wait_for_workers else 0,
        )
        self._monitor_btn.setText("开始监控")
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        self._status_label.setText("已停止")
        self._status_label.setStyleSheet("color: #888;")
        self._log_message("监控已停止")
        self._refresh_status_cards()

    def _stop_monitor_workers(self, timeout_ms: int | None) -> bool:
        """Stop all detector workers and return whether they exited cleanly."""
        workers = list(getattr(self, "_workers", {}).values())
        legacy_worker = getattr(self, "_worker", None)
        if legacy_worker is not None and legacy_worker not in workers:
            workers.append(legacy_worker)
        for worker in _instance_attr(self, "_stopping_monitor_workers", set()):
            if worker not in workers:
                workers.append(worker)
        if not workers:
            return True

        failed = False
        running_workers = [worker for worker in workers if worker.isRunning()]
        if running_workers:
            self._log_message(f"正在停止 {len(running_workers)} 个监控线程...")
        for worker in workers:
            worker.stop()

        if timeout_ms == 0:
            for worker in workers:
                self._disconnect_worker_signals(worker)
            self._stopping_monitor_workers = set(workers)
            self._workers = {}
            self._worker_contexts = {}
            self._worker = None
            self._monitor_btn.setEnabled(False)
            self._refresh_window_status_table()
            QTimer.singleShot(50, self._reap_stopping_monitor_workers)
            return True

        for worker in workers:
            if worker.isRunning():
                stopped = (
                    worker.wait()
                    if timeout_ms is None
                    else worker.wait(timeout_ms)
                )
                if not stopped:
                    failed = True
                    logger.warning(
                        "Worker thread did not stop within %s ms timeout",
                        timeout_ms,
                    )
            self._disconnect_worker_signals(worker)

        self._stopping_monitor_workers = set()
        if failed:
            self._capturer = Capturer()
            self._ocr = OCREngine(lang="en", confidence_threshold=0.7)
        self._workers = {}
        self._worker_contexts = {}
        self._worker = None
        self._refresh_window_status_table()
        return not failed

    def _reap_stopping_monitor_workers(self) -> None:
        """Release stopped monitor threads without blocking the Qt event loop."""
        stopping = _instance_attr(self, "_stopping_monitor_workers", set())
        self._stopping_monitor_workers = {
            worker for worker in stopping if worker.isRunning()
        }
        if self._stopping_monitor_workers:
            QTimer.singleShot(50, self._reap_stopping_monitor_workers)
            return
        self._monitor_btn.setEnabled(True)

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
        try:
            return IntelApiClient(
                self._intel_url,
                timeout=timeout,
                api_key=self._settings.get_api_key(),
            )
        except IntelApiError as exc:
            self._settings.set_auth_status(str(exc), error=True)
            logger.warning("Could not create authenticated intel client: %s", exc)
            return None

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

    def _publish_heartbeat(
        self,
        *,
        monitoring_override: bool | None = None,
        task_key: str = "heartbeat",
    ) -> None:
        if (
            self._intel_client is None
            or not _instance_attr(self, "_uploads_enabled", True)
        ):
            return
        monitoring = (
            self._is_monitoring()
            if monitoring_override is None
            else bool(monitoring_override)
        )
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
                    "monitoring": monitoring
                    and context["key"] in getattr(self, "_workers", {}),
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
                task_key,
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
        """Hide immediately while background workers unwind asynchronously."""
        self._quit_app()
        event.ignore()

    def _quit_app(self):
        if _instance_attr(self, "_shutdown_in_progress", False):
            return
        self._shutdown_in_progress = True
        self.hide()
        tray = _instance_attr(self, "_tray")
        if tray is not None:
            tray.hide()
        self._stop_monitor(wait_for_workers=False)
        self._stop_alert(wait_for_worker=False)
        network_tasks = _instance_attr(self, "_network_tasks")
        if network_tasks is not None:
            network_tasks.shutdown()
        QTimer.singleShot(0, self._finish_quit_when_workers_stop)

    def _finish_quit_when_workers_stop(self) -> None:
        """Quit after Qt workers finish without blocking the event loop."""
        monitor_workers = _instance_attr(
            self,
            "_stopping_monitor_workers",
            set(),
        )
        alert_controllers = _instance_attr(
            self,
            "_stopping_alert_controllers",
            set(),
        )
        monitor_running = any(worker.isRunning() for worker in monitor_workers)
        alert_running = any(
            controller.is_running() for controller in alert_controllers
        )
        if monitor_running or alert_running:
            QTimer.singleShot(50, self._finish_quit_when_workers_stop)
            return
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


def _is_auth_rejection(message: str) -> bool:
    text = str(message or "").casefold()
    return any(
        token in text
        for token in (
            "unauthorized",
            "revoked",
            "disabled",
            "invalid api key",
            "authentication is required",
        )
    )


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


def _window_geometry_signature(window: dict) -> tuple:
    """Return the geometry fields that require a live capture-region remap."""
    return (
        int(window.get("x") or 0),
        int(window.get("y") or 0),
        int(window.get("w") or 0),
        int(window.get("h") or 0),
        str(window.get("monitor") or ""),
    )
