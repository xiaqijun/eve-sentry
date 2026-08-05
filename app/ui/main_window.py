"""Main application window."""

import io
import logging
import os
import threading
import time
from argparse import Namespace
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSettings, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCursor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
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
from app.core.client_identity import persistent_client_id
from app.diagnostics import default_log_path, export_diagnostic_bundle
from app.engine.capturer import Capturer
from app.core.heartbeat import (
    build_detector_heartbeat_details,
    heartbeat_now_iso,
    resolve_runtime_identity,
)
from app.engine.ocr import OCREngine
from app.engine.ocr_runtime import preload_ocr_runtime
from app.engine.ocr_scheduler import SharedOCRScheduler
from app.engine.worker import MonitorWorker
from app.intel_client import IntelApiClient, IntelApiError
from app.models.region_prefs import RegionPreferences
from app.persistent_intel_client import PersistentIntelApiClient
from app.ui.background_tasks import BackgroundTaskRunner
from app.ui.reliable_uploads import ReliableUploadManager
from app.ui.region_selector import RegionSelector, map_rect_between_geometries
from app.ui.settings import SettingsPanel
from app.ui.theme import APP_QSS, monitor_button_style, status_card_style
from app.startup import set_start_with_windows
from app.updater import ClientUpdater
from app.version import current_version

logger = logging.getLogger(__name__)


def _force_exit_if_shutdown_stalls(
    completed: threading.Event,
    timeout: float,
) -> None:
    """Guarantee process exit when a native worker ignores cancellation."""
    if completed.wait(max(1.0, float(timeout))):
        return
    logger.critical("Client shutdown exceeded %.1f seconds; forcing exit", timeout)
    os._exit(0)


class PreviewCaptureWorker(QThread):
    """Capture one background window region and return detached PNG bytes."""

    captured = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, window: dict, region: dict, parent=None) -> None:
        super().__init__(parent)
        self._window = dict(window)
        self._region = dict(region)

    def run(self) -> None:
        capturer = None
        image_data = b""
        error = ""
        try:
            capturer = Capturer()
            capturer.select_window(
                self._window["hwnd"],
                self._window.get("title", ""),
                self._window.get("w", 0),
                self._window.get("h", 0),
            )
            image = capturer.screenshot(
                self._region["x"],
                self._region["y"],
                self._region["w"],
                self._region["h"],
            )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            image_data = buffer.getvalue()
        except Exception as exc:
            error = str(exc)
        finally:
            if capturer is not None:
                capturer.close()
        if error:
            self.failed.emit(error)
        elif image_data:
            self.captured.emit(image_data)


class MultiSelectMenu(QMenu):
    """Keep the popup open while checkable monitor targets are toggled."""

    def mouseReleaseEvent(self, event) -> None:
        action = self.actionAt(event.position().toPoint())
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(900, 620)
        self.resize(980, 680)
        self.setStyleSheet(APP_QSS)
        self._runtime_settings = QSettings("EveSentry", "EVE Sentry Monitor")
        geometry = self._runtime_settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self._monitor_window_actions: dict[str, QAction] = {}
        self._monitor_windows_by_key: dict[str, dict] = {}
        self._monitor_window_titles_by_key: dict[str, str] = {}
        self._monitor_last_status_by_title: dict[str, dict[str, str]] = {}
        self._monitor_known_window_keys: set[str] = set()
        self._monitor_selected_titles = set(
            _string_list(
                self._runtime_settings.value(
                    "monitor/selected_window_titles",
                    [],
                )
            )
        )
        legacy_monitor_all = self._runtime_settings.value(
            "monitor/all_windows",
            False,
            type=bool,
        )
        self._monitor_select_all_new_windows = self._runtime_settings.value(
            "monitor/select_all",
            legacy_monitor_all,
            type=bool,
        )
        selection_policy_version = self._runtime_settings.value(
            "monitor/selection_policy_version",
            0,
            type=int,
        )
        if int(selection_policy_version or 0) < 1:
            self._monitor_selected_titles = set()
            self._monitor_select_all_new_windows = False
            self._runtime_settings.setValue(
                "monitor/selection_policy_version",
                1,
            )
        self._monitor_select_current_on_first_sync = False
        self._syncing_monitor_menu = False

        self._region_prefs = RegionPreferences()
        self._capturer = Capturer()
        self._ocr = None  # legacy test/tool injection point; models load on monitor start
        self._ocr_scheduler: SharedOCRScheduler | None = None
        self._worker: MonitorWorker | None = None
        self._workers: dict[str, MonitorWorker] = {}
        self._worker_contexts: dict[str, dict] = {}
        self._detected_window_contexts: dict[str, dict] = {}
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
        self._monitor_start_state = "idle"
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
        self._local_system_cache: dict[str, tuple[float, object | None]] = {}
        self._local_system_pending: set[str] = set()
        self._heartbeat_interval = _env_float(
            "EVE_SENTRY_HEARTBEAT_INTERVAL",
            default=15.0,
            minimum=5.0,
        )
        self._heartbeat_client_id = persistent_client_id("detector")
        self._heartbeat_runtime = resolve_runtime_identity()
        self._heartbeat_last_action = "startup"
        self._heartbeat_last_error = ""
        self._heartbeat_last_success_at = ""
        self._last_heartbeat_error = ""
        self._uploads_enabled = False
        self._intel_client = self._create_intel_client()
        self._upload_manager: ReliableUploadManager | None = None
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(int(self._heartbeat_interval * 1000))
        self._heartbeat_timer.timeout.connect(self._publish_heartbeat)
        self._window_refresh_timer = QTimer(self)
        self._window_refresh_timer.setInterval(3000)
        self._window_refresh_timer.timeout.connect(self._refresh_detected_windows)
        self._monitor_reconnect_scheduled = False
        self._network_tasks = BackgroundTaskRunner(max_workers=2, parent=self)
        self._network_tasks.completed.connect(self._on_network_task_completed)
        self._identity_timer = QTimer(self)
        self._identity_timer.setInterval(10000)
        self._identity_timer.timeout.connect(self._poll_identity_logs)
        self._identity_timer.start()
        self._alert_controller: AlertTrayController | None = None
        self._stopping_monitor_workers: set[MonitorWorker] = set()
        self._preview_capture_worker: PreviewCaptureWorker | None = None

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

        self._settings.scan_settings_changed.connect(self._apply_scan_settings)
        self._settings.server_url_changed.connect(self._apply_server_url)
        self._settings.api_key_changed.connect(self._apply_api_key)
        self._settings.behavior_settings_changed.connect(
            self._apply_behavior_settings
        )
        self._settings.diagnostics_requested.connect(self._export_diagnostics)
        self._settings.setFixedWidth(240)
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

        self._connection_label = QLabel(
            "重连中" if self._intel_client is not None else "未配置"
        )
        self._connection_label.setObjectName("authStatus")
        header_row.addWidget(self._connection_label)

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
        self._window_combo.setPlaceholderText("请选择 EVE 窗口")
        self._window_combo.currentIndexChanged.connect(self._on_window_selected)
        target_row.addWidget(self._window_combo, 1)

        self._monitor_window_button = QToolButton()
        self._monitor_window_button.setObjectName("monitorWindowButton")
        self._monitor_window_button.setMinimumSize(138, 34)
        self._monitor_window_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._monitor_window_button.setToolTip(
            "选择需要独立识别角色、星系和成员列表的 EVE 客户端"
        )
        self._monitor_window_menu = MultiSelectMenu(self._monitor_window_button)
        self._monitor_window_menu.setToolTipsVisible(True)
        self._monitor_window_button.setMenu(self._monitor_window_menu)
        self._refresh_monitor_window_button()
        target_row.addWidget(self._monitor_window_button)

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
        preview_btn = QPushButton("预览")
        preview_btn.setObjectName("secondaryAction")
        preview_btn.clicked.connect(self._preview_region)
        target_row.addWidget(preview_btn)
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
        self._window_status_table = QTableWidget(0, 6)
        self._window_status_table.setHorizontalHeaderLabels(
            ["角色", "星系", "窗口", "区域", "状态", "最近动作"]
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
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
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
        self._log.document().setMaximumBlockCount(1500)
        right.addWidget(self._log, 1)

        root.addWidget(workspace, 1)

        self._status = QStatusBar()
        self._status.setObjectName("appStatusBar")
        self.setStatusBar(self._status)
        self._status_label = QLabel("待机")
        self._status.addWidget(self._status_label)
        self._ocr_health_label = QLabel("OCR 未加载")
        self._status.addPermanentWidget(self._ocr_health_label)
        self._ocr_health_timer = QTimer(self)
        self._ocr_health_timer.setInterval(2000)
        self._ocr_health_timer.timeout.connect(self._refresh_ocr_health)
        self._ocr_health_timer.start()

        self._reset_upload_manager()
        self._setup_tray()
        self._detect_window()
        self._window_refresh_timer.start()
        self._refresh_window_status_table()
        self._refresh_status_cards()
        if self._settings.get_api_key():
            QTimer.singleShot(0, self._begin_identity_check)
        restore_monitoring = bool(
            self._runtime_settings.value("monitor/was_running", False, type=bool)
        )
        if _env_flag("EVE_SENTRY_AUTO_START_MONITOR", default=False) or (
            self._settings.get_restore_monitor_state() and restore_monitoring
        ):
            QTimer.singleShot(0, self._auto_start_monitor)
        QTimer.singleShot(350, self._preload_ocr_runtime)
        QTimer.singleShot(1500, self._updater.check)

    def _preload_ocr_runtime(self) -> None:
        """Warm Python OCR imports off the UI thread while keeping models lazy."""
        runner = _instance_attr(self, "_network_tasks")
        if runner is None:
            return
        runner.submit_once(
            "ocr-runtime-preload",
            preload_ocr_runtime,
            {"kind": "ocr_runtime_preload"},
        )

    def _auto_start_monitor(self) -> None:
        """Start monitoring once the event loop is ready when explicitly requested."""
        if self._monitor_btn.isChecked():
            return
        self._monitor_btn.setChecked(True)
        self._start_monitor()

    def should_start_minimized(self) -> bool:
        """Return whether the configured startup should remain in the tray."""
        return self._settings.get_start_minimized()

    def activate_window(self) -> None:
        """Bring the existing client window to the foreground."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

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
        actions = _instance_attr(self, "_monitor_window_actions", {})
        online_keys = set(
            _instance_attr(self, "_monitor_windows_by_key", {})
        )
        selected_count = sum(
            1
            for key, action in actions.items()
            if key in online_keys and action.isChecked()
        )
        online_count = len(online_keys)
        self._set_status_card(
            "ocr",
            (
                f"{worker_count} 窗口监控中"
                if monitoring
                else ("等待启动" if selected_count else "未选择窗口")
            ),
            "active" if monitoring else ("idle" if selected_count else "warn"),
        )

        if monitoring:
            self._set_status_card("window", f"{worker_count} 个窗口", "active")
        elif online_count:
            self._set_status_card(
                "window",
                f"已选 {selected_count}/{online_count}",
                "ok" if selected_count else "warn",
            )
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

        detected_contexts = dict(
            _instance_attr(self, "_detected_window_contexts", {})
        )
        detected_contexts.update(_instance_attr(self, "_worker_contexts", {}))
        contexts = list(detected_contexts.values())
        rows: list[dict] = []
        if contexts:
            rows = contexts
        else:
            window_combo = getattr(self, "_window_combo", None)
            title = window_combo.currentText().strip() if window_combo else ""
            region = getattr(self, "_manual_region", None) or getattr(self, "_detected_region", None)
            rows = [
                {
                    "character_name": _character_name_from_window_title(title),
                    "system_name": "Unknown",
                    "window_title": title or "未检测到 EVE 窗口",
                    "region": region,
                    "runtime_status": "待启动" if title else "未检测",
                    "last_action": "窗口已选择，监控尚未启动" if title else "点击刷新或确认 EVE 已启动",
                }
            ]

        table.setRowCount(len(rows))
        for row_index, context in enumerate(rows):
            system_name = str(context.get("system_name") or "Unknown")
            system_label = (
                "未知" if system_name.casefold() == "unknown" else system_name
            )
            values = [
                str(context.get("character_name") or "-"),
                system_label,
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
        self._remember_monitor_window_context(context)
        self._refresh_monitor_window_action_labels()
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
        user_id = str(
            window.get("character_id")
            or window.get("user_id")
            or window.get("character_name")
            or ""
        ).strip()
        raw = f"user:{user_id}" if user_id else self._window_monitor_key(window)
        slug = "".join(
            char if ("a" <= char <= "z" or "0" <= char <= "9") else "-"
            for char in raw.lower()
        ).strip("-")
        slug = "-".join(part for part in slug.split("-") if part)
        return f"{self._heartbeat_client_id}:{slug or 'window'}"

    def _build_monitor_targets(self) -> list[dict]:
        """Build independent targets for the selected EVE windows."""
        keyword = self._settings.get_keyword()
        detected_windows = self._capturer.list_eve_windows(keyword)
        windows = self._selected_monitor_windows(detected_windows)

        if not windows:
            return []

        configured_system = (
            _instance_attr(self, "_intel_system", "Unknown")
            if _instance_attr(self, "_intel_system_source", "default") == "env"
            else "Unknown"
        )
        character_ids_by_name: dict[str, int] = {}
        auth_state_store = getattr(self._settings, "auth_state_store", None)
        if callable(auth_state_store):
            state = auth_state_store().load()
            for identity in state.get("character_identities", []):
                if not isinstance(identity, dict):
                    continue
                character_name = str(
                    identity.get("character_name") or ""
                ).strip()
                try:
                    character_id = int(identity.get("character_id"))
                except (TypeError, ValueError):
                    continue
                if character_name and character_id > 0:
                    character_ids_by_name[character_name.casefold()] = character_id
        targets: list[dict] = []
        for window in windows:
            region_prefs = _instance_attr(self, "_region_prefs")
            region = (
                region_prefs.resolve_region(window)
                if region_prefs is not None
                else None
            )
            key = self._window_monitor_key(window)
            window_title = str(window.get("title") or key)
            character_name = _character_name_from_window_title(window_title)
            character_id = character_ids_by_name.get(character_name.casefold())
            client_window = dict(window)
            if character_id is not None:
                client_window["character_id"] = character_id
            if character_name:
                client_window["character_name"] = character_name
            targets.append(
                {
                    "key": key,
                    "client_id": self._window_client_id(client_window),
                    "window": dict(window),
                    "window_title": window_title,
                    "character_id": character_id,
                    "character_name": character_name,
                    "source_instance": self._window_combo_label(window),
                    "region": dict(region) if region else None,
                    "system_name": configured_system,
                    "system_id": None,
                    "system_source": (
                        "env" if configured_system != "Unknown" else "default"
                    ),
                    "_location_next_check": 0.0,
                }
            )
        return targets

    def _selected_monitor_windows(self, windows: list[dict]) -> list[dict]:
        """Return selected windows with the current calibration target first."""
        actions = _instance_attr(self, "_monitor_window_actions")
        if actions is None:
            selected_window = self._current_window_info()
            return [selected_window] if selected_window is not None else []

        selected_keys = {
            key for key, action in actions.items() if action.isChecked()
        }
        selected = [
            window
            for window in windows
            if self._window_monitor_key(window) in selected_keys
        ]
        window_combo = _instance_attr(self, "_window_combo")
        current_hwnd = window_combo.currentData() if window_combo is not None else None
        selected.sort(
            key=lambda window: 0 if window.get("hwnd") == current_hwnd else 1
        )
        return selected

    def _sync_monitor_window_menu(self, windows: list[dict]) -> None:
        """Rebuild the checkable monitor target menu without losing intent."""
        menu = _instance_attr(self, "_monitor_window_menu")
        if menu is None:
            return
        previous_actions = _instance_attr(self, "_monitor_window_actions", {})
        previous_checked_keys = {
            key for key, action in previous_actions.items() if action.isChecked()
        }
        previous_known_keys = set(
            _instance_attr(self, "_monitor_known_window_keys", set())
        )
        selected_titles = set(
            _instance_attr(self, "_monitor_selected_titles", set())
        )
        selected_title_keys = {title.casefold() for title in selected_titles}
        select_current = bool(
            _instance_attr(self, "_monitor_select_current_on_first_sync", False)
        )
        window_combo = _instance_attr(self, "_window_combo")
        current_hwnd = window_combo.currentData() if window_combo is not None else None

        self._syncing_monitor_menu = True
        menu.clear()
        select_all_action = menu.addAction("全选")
        select_all_action.triggered.connect(self._select_all_monitor_windows)
        current_only_action = menu.addAction("仅选当前窗口")
        current_only_action.triggered.connect(self._select_current_monitor_window)
        menu.addSeparator()

        actions: dict[str, QAction] = {}
        windows_by_key: dict[str, dict] = {}
        titles_by_key: dict[str, str] = {}
        online_titles: set[str] = set()
        if windows:
            title_counts: dict[str, int] = {}
            for window in windows:
                title = str(window.get("title") or "").strip() or "EVE 窗口"
                title_key = title.casefold()
                title_counts[title_key] = title_counts.get(title_key, 0) + 1
            title_indexes: dict[str, int] = {}
            for window in windows:
                key = self._window_monitor_key(window)
                title = str(window.get("title") or "").strip() or "EVE 窗口"
                title_key = title.casefold()
                title_indexes[title_key] = title_indexes.get(title_key, 0) + 1
                action = menu.addAction(
                    self._window_combo_label(
                        window,
                        duplicate_index=title_indexes[title_key],
                        duplicate_count=title_counts[title_key],
                    )
                )
                action.setCheckable(True)
                action.setChecked(
                    (select_current and window.get("hwnd") == current_hwnd)
                    or key in previous_checked_keys
                    or (
                        key not in previous_known_keys
                        and title.casefold() in selected_title_keys
                    )
                )
                action.triggered.connect(self._on_monitor_window_toggled)
                actions[key] = action
                windows_by_key[key] = dict(window)
                titles_by_key[key] = title
                online_titles.add(title)

        online_title_keys = {title.casefold() for title in online_titles}
        cached_status = _instance_attr(self, "_monitor_last_status_by_title", {})
        self._monitor_last_status_by_title = {
            title: status
            for title, status in cached_status.items()
            if title.casefold() in online_title_keys
        }
        if not windows:
            placeholder = menu.addAction("未检测到 EVE 窗口")
            placeholder.setEnabled(False)

        self._monitor_window_actions = actions
        self._monitor_windows_by_key = windows_by_key
        self._monitor_window_titles_by_key = titles_by_key
        self._monitor_known_window_keys = set(windows_by_key)
        self._syncing_monitor_menu = False
        if select_current and actions:
            self._monitor_select_current_on_first_sync = False
        self._persist_monitor_window_selection(update_select_all=False)
        self._refresh_monitor_window_action_labels()

    def _remember_monitor_window_context(self, context: dict | None) -> None:
        """Cache the last useful system and status for an EVE window title."""
        if not context:
            return
        title = str(context.get("window_title") or "").strip()
        if not title:
            return
        cache = _instance_attr(self, "_monitor_last_status_by_title", {})
        previous = dict(cache.get(title) or {})
        system_name = str(context.get("system_name") or "Unknown").strip()
        runtime_status = str(context.get("runtime_status") or "").strip()
        if system_name and system_name.casefold() != "unknown":
            previous["system_name"] = system_name
        if runtime_status:
            previous["runtime_status"] = runtime_status
        cache[title] = previous
        self._monitor_last_status_by_title = cache

    def _refresh_monitor_window_action_labels(self) -> None:
        """Show character, system and runtime state on every target action."""
        actions = _instance_attr(self, "_monitor_window_actions", {})
        windows_by_key = _instance_attr(self, "_monitor_windows_by_key", {})
        titles_by_key = _instance_attr(
            self,
            "_monitor_window_titles_by_key",
            {},
        )
        contexts = _instance_attr(self, "_worker_contexts", {})
        for context in contexts.values():
            self._remember_monitor_window_context(context)
        cached_status = _instance_attr(
            self,
            "_monitor_last_status_by_title",
            {},
        )

        contexts_by_title = {
            str(context.get("window_title") or "").strip(): context
            for context in contexts.values()
            if str(context.get("window_title") or "").strip()
        }
        monitor_btn = _instance_attr(self, "_monitor_btn")
        is_monitor_checked = getattr(monitor_btn, "isChecked", None)
        monitoring = bool(
            callable(is_monitor_checked) and is_monitor_checked()
        )
        for key, action in actions.items():
            title = str(titles_by_key.get(key) or "EVE 窗口").strip()
            character_name = _character_name_from_window_title(title) or title
            online = key in windows_by_key
            context = contexts.get(key) or contexts_by_title.get(title)
            cached = cached_status.get(title) or {}
            system_name = str(
                (context or {}).get("system_name")
                or cached.get("system_name")
                or "Unknown"
            ).strip()
            system_label = (
                "未知" if system_name.casefold() == "unknown" else system_name
            )
            if not online:
                status = "离线"
            elif not action.isChecked():
                status = "未监控"
            elif context is not None:
                status = str(context.get("runtime_status") or "准备中")
            else:
                status = "准备中" if monitoring else "待启动"
            set_property = getattr(action, "setProperty", None)
            if callable(set_property):
                set_property("online", online)
                set_property("characterName", character_name)
                set_property("systemName", system_label)
                set_property("runtimeStatus", status)
            set_text = getattr(action, "setText", None)
            if callable(set_text):
                set_text(f"{character_name} · {system_label} · {status}")
            set_tooltip = getattr(action, "setToolTip", None)
            if not callable(set_tooltip):
                continue
            if online:
                set_tooltip(f"{title}\n当前星系：{system_label}；状态：{status}")
            else:
                set_tooltip(
                    f"{title}\n窗口当前离线；保持勾选后，重新出现时会自动恢复监控"
                )
        self._refresh_monitor_window_button()

    def _refresh_monitor_window_button(self) -> None:
        """Show the number and names of currently selected monitor targets."""
        button = _instance_attr(self, "_monitor_window_button")
        if button is None:
            return
        actions = _instance_attr(self, "_monitor_window_actions", {})
        windows_by_key = _instance_attr(self, "_monitor_windows_by_key", {})
        titles_by_key = _instance_attr(
            self,
            "_monitor_window_titles_by_key",
            {},
        )
        online_keys = set(windows_by_key)
        selected_online_keys = [
            key
            for key in online_keys
            if key in actions and actions[key].isChecked()
        ]
        text = f"监控窗口 {len(selected_online_keys)}/{len(online_keys)}"
        button.setText(text)

        selected_titles = [
            titles_by_key.get(key, actions[key].text())
            for key in selected_online_keys
        ]
        if selected_titles:
            tooltip = "已选择：" + "、".join(selected_titles)
            selection_state = "ready"
        elif online_keys:
            tooltip = "尚未选择监控窗口，请打开菜单进行勾选"
            selection_state = "empty"
        else:
            tooltip = "未检测到 EVE 窗口"
            selection_state = "empty"
        button.setToolTip(tooltip)
        if button.property("selectionState") != selection_state:
            button.setProperty("selectionState", selection_state)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _persist_monitor_window_selection(
        self,
        *,
        update_select_all: bool = True,
    ) -> None:
        """Persist selected titles while retaining temporarily missing targets."""
        actions = _instance_attr(self, "_monitor_window_actions", {})
        windows_by_key = _instance_attr(self, "_monitor_windows_by_key", {})
        titles_by_key = _instance_attr(
            self,
            "_monitor_window_titles_by_key",
            {},
        )
        # Keep selections for temporarily offline windows. They are restored
        # when the same character window reappears; only an explicit uncheck
        # removes a title from the checkpoint.
        selected_titles: set[str] = set(
            _instance_attr(self, "_monitor_selected_titles", set())
        )
        for key, action in actions.items():
            if key not in windows_by_key:
                continue
            title = str(titles_by_key.get(key) or "").strip()
            if not title:
                continue
            matching_titles = {
                item for item in selected_titles if item.casefold() == title.casefold()
            }
            if action.isChecked():
                selected_titles.add(title)
            else:
                selected_titles.difference_update(matching_titles)
        self._monitor_selected_titles = selected_titles
        online_keys = set(windows_by_key)
        select_all_new_windows = bool(
            _instance_attr(self, "_monitor_select_all_new_windows", False)
        )
        if update_select_all and online_keys:
            select_all_new_windows = all(
                key in actions and actions[key].isChecked()
                for key in online_keys
            )
            self._monitor_select_all_new_windows = select_all_new_windows
        runtime_settings = _instance_attr(self, "_runtime_settings")
        if runtime_settings is not None:
            runtime_settings.setValue(
                "monitor/selected_window_titles",
                sorted(selected_titles),
            )
            runtime_settings.setValue(
                "monitor/select_all",
                select_all_new_windows,
            )

    def _on_monitor_window_toggled(self, _checked: bool = False) -> None:
        """Persist a target change and reconcile running monitor workers."""
        if _instance_attr(self, "_syncing_monitor_menu", False):
            return
        self._persist_monitor_window_selection()
        windows = list(
            _instance_attr(self, "_monitor_windows_by_key", {}).values()
        )
        if windows:
            self._refresh_detected_window_contexts(windows)
        else:
            self._refresh_monitor_window_action_labels()
            self._refresh_window_status_table()
        self._refresh_status_cards()
        monitor_btn = _instance_attr(self, "_monitor_btn")
        if monitor_btn is None or not monitor_btn.isChecked():
            return
        if not any(
            key in _instance_attr(self, "_monitor_windows_by_key", {})
            and action.isChecked()
            for key, action in _instance_attr(
                self,
                "_monitor_window_actions",
                {},
            ).items()
        ):
            self._log_message("未选择监控窗口，已停止监控")
            self._stop_monitor()
            return
        self._log_message("监控窗口选择已更新，正在重新连接")
        QTimer.singleShot(0, lambda: self._start_monitor(identity_checked=True))

    def _select_all_monitor_windows(self) -> None:
        """Select every currently detected EVE window."""
        self._syncing_monitor_menu = True
        for action in _instance_attr(self, "_monitor_window_actions", {}).values():
            action.setChecked(True)
        self._syncing_monitor_menu = False
        self._on_monitor_window_toggled(True)

    def _select_current_monitor_window(self) -> None:
        """Select only the window currently used for calibration and preview."""
        window_combo = _instance_attr(self, "_window_combo")
        current_hwnd = window_combo.currentData() if window_combo is not None else None
        windows_by_key = _instance_attr(self, "_monitor_windows_by_key", {})
        self._syncing_monitor_menu = True
        for key, action in _instance_attr(self, "_monitor_window_actions", {}).items():
            action.setChecked(
                (windows_by_key.get(key) or {}).get("hwnd") == current_hwnd
            )
        self._syncing_monitor_menu = False
        self._on_monitor_window_toggled(True)

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
        if windows is None or isinstance(windows, bool):
            keyword = self._settings.get_keyword()
            windows = self._capturer.list_eve_windows(keyword)
        windows = _titled_windows(windows)
        self._window_signature = _window_list_signature(windows)
        self._window_combo.blockSignals(True)
        self._window_combo.clear()
        selected_index = -1
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
        self._sync_monitor_window_menu(windows)
        self._refresh_detected_window_contexts(windows)
        self._sync_monitor_window_status(windows)
        self._refresh_status_cards()
        self._refresh_window_status_table()

    def _refresh_detected_windows(self) -> None:
        """Refresh the selector when the set of EVE windows changes."""
        keyword = self._settings.get_keyword()
        windows = _titled_windows(self._capturer.list_eve_windows(keyword))
        self._sync_monitor_target_geometry(windows)
        self._sync_monitor_window_status(windows)
        signature = _window_list_signature(windows)
        if signature != _instance_attr(self, "_window_signature", ()):
            self._detect_window(windows=windows)
        else:
            self._refresh_detected_window_contexts(windows)
        selected_keys = {
            self._window_monitor_key(window)
            for window in self._selected_monitor_windows(windows)
        }
        self._schedule_monitor_reconnect(expected_keys=selected_keys)

    def _refresh_detected_window_contexts(self, windows: list[dict]) -> None:
        """Resolve display state for all running windows before monitoring starts."""
        previous_contexts = _instance_attr(self, "_detected_window_contexts", {})
        worker_contexts = _instance_attr(self, "_worker_contexts", {})
        cached_status = _instance_attr(self, "_monitor_last_status_by_title", {})
        actions = _instance_attr(self, "_monitor_window_actions", {})
        window_combo = _instance_attr(self, "_window_combo")
        current_hwnd = window_combo.currentData() if window_combo is not None else None
        configured_system = (
            _instance_attr(self, "_intel_system", "Unknown")
            if _instance_attr(self, "_intel_system_source", "default") == "env"
            else "Unknown"
        )
        contexts: dict[str, dict] = {}
        for window in windows:
            key = self._window_monitor_key(window)
            running = worker_contexts.get(key)
            title = str(window.get("title") or key).strip()
            previous = dict(running or previous_contexts.get(key) or {})
            cached = cached_status.get(title) or {}
            region_prefs = _instance_attr(self, "_region_prefs")
            region = (
                region_prefs.resolve_region(window)
                if region_prefs is not None
                else None
            )
            action = actions.get(key)
            selected = (
                action.isChecked()
                if action is not None
                else window.get("hwnd") == current_hwnd
            )
            previous_system = str(
                previous.get("system_name")
                or cached.get("system_name")
                or configured_system
                or "Unknown"
            ).strip()
            context = {
                **previous,
                "key": key,
                "window": dict(window),
                "window_title": title,
                "character_name": _character_name_from_window_title(title),
                "region": dict(region) if region else None,
                "system_name": previous_system or "Unknown",
                "system_id": previous.get("system_id"),
                "system_source": previous.get("system_source")
                or ("env" if configured_system != "Unknown" else "default"),
                "runtime_status": "待启动" if selected else "未选择",
                "last_action": (
                    "窗口已选择，监控尚未启动"
                    if selected
                    else "未选择为监控窗口"
                ),
                "_location_next_check": float(
                    previous.get("_location_next_check") or 0.0
                ),
            }
            if _instance_attr(self, "_use_local_system_log", False):
                self._refresh_intel_location(context=context)
            contexts[key] = context
        self._detected_window_contexts = contexts
        self._refresh_monitor_window_action_labels()
        self._refresh_window_status_table()

    def _sync_monitor_window_status(self, windows: list[dict]) -> None:
        """Keep the global status aligned with the worker, not transient geometry."""
        monitor_btn = _instance_attr(self, "_monitor_btn")
        status_label = _instance_attr(self, "_status_label")
        if (
            monitor_btn is None
            or status_label is None
            or not monitor_btn.isChecked()
        ):
            return
        if self._running_workers():
            status_label.setText("监控中")
            status_label.setStyleSheet("color: #37d6b0; font-weight: bold;")
        elif not windows:
            status_label.setText("等待 EVE 窗口重新出现")
            status_label.setStyleSheet("color: #f0b35a; font-weight: bold;")

    def _schedule_monitor_reconnect(
        self,
        *,
        expected_keys: set[str] | None = None,
    ) -> None:
        """Rebuild monitoring when selected and running target sets diverge."""
        monitor_btn = _instance_attr(self, "_monitor_btn")
        if monitor_btn is None or not monitor_btn.isChecked():
            return
        if _instance_attr(self, "_stopping_monitor_workers", set()):
            return
        if expected_keys is None:
            expected_keys = {
                target["key"] for target in self._build_monitor_targets()
            }
        running_keys = {
            key
            for key, worker in _instance_attr(self, "_workers", {}).items()
            if worker is not None and worker.isRunning()
        }
        if not expected_keys or running_keys == expected_keys:
            return
        if _instance_attr(self, "_monitor_reconnect_scheduled", False):
            return
        self._monitor_reconnect_scheduled = True
        QTimer.singleShot(0, self._reconnect_monitor)

    def _reconnect_monitor(self) -> None:
        self._monitor_reconnect_scheduled = False
        monitor_btn = _instance_attr(self, "_monitor_btn")
        if monitor_btn is None or not monitor_btn.isChecked():
            return
        targets = self._build_monitor_targets()
        if not targets:
            return
        expected_keys = {target["key"] for target in targets}
        running_keys = {
            key
            for key, worker in _instance_attr(self, "_workers", {}).items()
            if worker is not None and worker.isRunning()
        }
        if running_keys == expected_keys:
            return
        self._log_message("EVE 窗口状态已恢复，正在重建监控节点")
        if self._running_workers():
            self._monitor_restart_pending = True
            self._stop_monitor_workers(timeout_ms=0)
            if not _instance_attr(self, "_stopping_monitor_workers", set()):
                self._reap_stopping_monitor_workers()
            return
        self._start_monitor(identity_checked=True)

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
            self._manual_region = None
            self._detected_region = None
            close = getattr(self._capturer, "close", None)
            if callable(close):
                close()
            self._window_label.setText("窗口：未选择")
            self._refresh_status_cards()
            self._refresh_window_status_table()
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
        self._detected_region = member
        self._set_alert_anchor(info)
        if member:
            self._window_label.setText(
                f"窗口：{self._window_combo.currentText()} -> 成员列表 {member['w']}x{member['h']}"
            )
        else:
            self._window_label.setText(
                f"窗口：{self._window_combo.currentText()} -> 未选择区域"
            )
        runtime_settings = _instance_attr(self, "_runtime_settings")
        if runtime_settings is not None:
            runtime_settings.setValue("monitor/window_title", info.get("title", ""))
        windows = list(
            _instance_attr(self, "_monitor_windows_by_key", {}).values()
        )
        if windows:
            self._refresh_detected_window_contexts(windows)
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
            QMessageBox.warning(self, "选择区域", "请先在窗口下拉框中选择 EVE 窗口。")
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

        selector_geometry = {
            "x": int(info["x"]),
            "y": int(info["y"]),
            "w": int(info["w"]),
            "h": int(info["h"]),
        }
        physical_geometry: dict[str, int] | None = None
        monitor_geometry_getter = getattr(
            self._capturer,
            "get_monitor_geometry",
            None,
        )
        monitor_geometry = (
            monitor_geometry_getter(info["hwnd"])
            if callable(monitor_geometry_getter)
            else None
        )
        screen = self._screen_for_monitor_geometry(monitor_geometry)
        if screen is not None and monitor_geometry is not None:
            logical_screen = screen.geometry()
            selector_geometry = map_rect_between_geometries(
                selector_geometry,
                monitor_geometry,
                {
                    "x": logical_screen.x(),
                    "y": logical_screen.y(),
                    "w": logical_screen.width(),
                    "h": logical_screen.height(),
                },
            )
            physical_geometry = {
                "x": int(info["x"]),
                "y": int(info["y"]),
                "w": int(info["w"]),
                "h": int(info["h"]),
            }

        selector_options = {"title": info["title"]}
        if physical_geometry is not None:
            selector_options["physical_geometry"] = physical_geometry

        self.hide()
        self._selector = RegionSelector(
            selector_geometry["x"],
            selector_geometry["y"],
            selector_geometry["w"],
            selector_geometry["h"],
            **selector_options,
        )
        self._selector.region_selected.connect(self._on_region_selected)
        self._selector.selector_closed.connect(self._on_selector_closed)
        self._selector.show()

    def _screen_for_monitor_geometry(self, monitor_geometry: dict | None):
        """Match physical Win32 monitor bounds to a Qt logical screen."""
        screens = QApplication.screens()
        if not screens or not monitor_geometry:
            return None
        primary = QApplication.primaryScreen()
        if monitor_geometry.get("primary") and primary is not None:
            return primary

        def match_score(screen) -> tuple[int, int]:
            geometry = screen.geometry()
            ratio = max(0.1, float(screen.devicePixelRatio()))
            physical_width = int(round(geometry.width() * ratio))
            physical_height = int(round(geometry.height() * ratio))
            size_error = abs(physical_width - int(monitor_geometry["w"]))
            size_error += abs(physical_height - int(monitor_geometry["h"]))
            origin_error = abs(geometry.x() - int(monitor_geometry["x"]))
            origin_error += abs(geometry.y() - int(monitor_geometry["y"]))
            return size_error, origin_error

        return min(screens, key=match_score)

    def _preview_region(self) -> None:
        """Show the exact capture area without initializing the OCR model."""
        region = self._manual_region or self._detected_region
        if not region:
            QMessageBox.information(self, "识别区域预览", "请先选择 EVE 窗口或识别区域。")
            return
        worker = _instance_attr(self, "_preview_capture_worker")
        if worker is not None and worker.isRunning():
            return

        window = self._current_window_info()
        if window is None:
            QMessageBox.warning(self, "预览失败", "当前 EVE 窗口不可用，请重新检测窗口。")
            return

        self._start_preview_capture(window, region)

    def _start_preview_capture(self, window: dict, region: dict) -> None:
        if _instance_attr(self, "_shutdown_in_progress", False):
            return
        worker = PreviewCaptureWorker(window, region, self)
        self._preview_capture_worker = worker
        worker.captured.connect(self._show_region_preview)
        worker.failed.connect(self._show_preview_error)
        worker.finished.connect(
            lambda worker=worker: self._on_preview_capture_finished(worker)
        )
        worker.start()

    def _show_region_preview(self, image_data: bytes) -> None:
        """Render a captured preview frame on the GUI thread."""
        if _instance_attr(self, "_shutdown_in_progress", False):
            return
        try:
            from PyQt6.QtGui import QPixmap

            pixmap = QPixmap()
            if not pixmap.loadFromData(image_data, "PNG"):
                raise RuntimeError("预览图片解码失败。")
        except Exception as exc:
            self._show_preview_error(str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("OCR 识别区域预览")
        dialog.resize(min(720, pixmap.width() + 24), min(760, pixmap.height() + 24))
        layout = QVBoxLayout(dialog)
        preview = QLabel()
        preview.setPixmap(pixmap)
        layout.addWidget(preview)
        dialog.show()
        self._region_preview_dialog = dialog

    def _show_preview_error(self, message: str) -> None:
        if not _instance_attr(self, "_shutdown_in_progress", False):
            QMessageBox.warning(self, "预览失败", message)

    def _on_preview_capture_finished(self, worker: PreviewCaptureWorker) -> None:
        if _instance_attr(self, "_preview_capture_worker") is worker:
            self._preview_capture_worker = None
        worker.deleteLater()

    def _refresh_ocr_health(self) -> None:
        label = _instance_attr(self, "_ocr_health_label")
        if label is None:
            return
        scheduler = _instance_attr(self, "_ocr_scheduler")
        if scheduler is None:
            label.setText("OCR 未加载")
            return
        health = scheduler.health()
        if health["state"] == "loading":
            label.setText("OCR 加载中")
        elif health["failed"]:
            label.setText(f"OCR 异常 {health['failed']} 次")
        else:
            label.setText(
                f"OCR 就绪 · {health['models_loaded']}/{health['max_instances']} 模型"
                f" · {health['last_latency_ms']:.0f} ms"
            )

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
            if _instance_attr(self, "_monitor_start_state", "idle") == "failed":
                self._monitor_start_state = "idle"
            self._start_monitor()
        else:
            self._monitor_start_state = "idle"
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

    def _detected_system_for_character(self, character_name: str) -> str:
        """Return the already resolved system for one locally detected character."""
        expected = str(character_name or "").strip().casefold()
        if not expected:
            return "Unknown"
        contexts = dict(_instance_attr(self, "_detected_window_contexts", {}))
        contexts.update(_instance_attr(self, "_worker_contexts", {}))
        for context in contexts.values():
            character = str(context.get("character_name") or "").strip().casefold()
            system_name = str(context.get("system_name") or "Unknown").strip()
            if (
                character == expected
                and system_name
                and system_name.casefold() != "unknown"
            ):
                return system_name
        return "Unknown"

    def _start_alert(self, *, identity_checked: bool = False) -> None:
        """Start the server-side warning consumer inside the monitor client."""
        if self._alert_controller is not None:
            return
        if _instance_attr(self, "_intel_url", None) == "":
            self._settings.set_auth_status("请先填写服务端地址", error=True)
            self._alert_btn.setChecked(False)
            return
        if not identity_checked:
            self._begin_identity_check("alert")
            return
        app = QApplication.instance()
        if app is None:
            self._alert_btn.setChecked(False)
            return
        settings = _instance_attr(self, "_settings")
        alert_preferences = (
            settings.get_alert_preferences()
            if settings is not None and hasattr(settings, "get_alert_preferences")
            else {}
        )
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
            alert_volume=alert_preferences.get("volume", 1.0),
            alert_muted=alert_preferences.get("muted", False),
            alert_cooldown=15.0,
            alert_repeat_interval=alert_preferences.get("repeat_interval", 2.0),
            alert_repeat_count=alert_preferences.get("repeat_count", 3),
        )
        try:
            controller = AlertTrayController(
                app,
                args,
                api_factory=PersistentIntelApiClient,
                tray_enabled=False,
                notification_callback=None,
                window_provider=lambda: self._capturer.list_eve_windows(
                    self._settings.get_keyword()
                ),
                system_resolver=self._detected_system_for_character,
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
        if server_url == self._intel_url:
            return

        restart_alert = self._alert_controller is not None
        if restart_alert:
            self._stop_alert()
        self._intel_url = server_url
        self._intel_client = self._create_intel_client()
        self._reset_upload_manager()
        self._api_key_validated = False
        self._last_heartbeat_error = ""
        self._heartbeat_last_error = ""
        self._log_message(
            f"服务端地址已更新：{server_url}"
            if server_url
            else "服务端地址已清除，网络功能已停用"
        )

        if restart_alert and server_url:
            self._start_alert()
        elif restart_alert:
            self._alert_btn.setChecked(False)
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
        self._reset_upload_manager()
        self._api_key_validated = False
        self._identity_wants_monitor = False
        self._identity_wants_alert = False
        self._settings.set_auth_status(
            "等待身份校验" if self._settings.get_api_key() else "未启用认证"
        )
        if self._settings.get_api_key():
            QTimer.singleShot(0, self._begin_identity_check)

    def _apply_behavior_settings(self) -> None:
        """Apply startup integration immediately after a preference change."""
        try:
            set_start_with_windows(self._settings.get_start_with_windows())
        except OSError as exc:
            self._log_message(f"无法更新开机启动设置：{exc}")

    def _export_diagnostics(self) -> None:
        """Export version, OCR health, window state and redacted logs."""
        default_name = f"eve-sentry-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip"
        target, _filter = QFileDialog.getSaveFileName(
            self,
            "导出诊断包",
            str(Path.home() / "Desktop" / default_name),
            "ZIP archive (*.zip)",
        )
        if not target:
            return
        scheduler = _instance_attr(self, "_ocr_scheduler")
        diagnostics = {
            "version": current_version(),
            "connection_state": getattr(
                _instance_attr(self, "_upload_manager"),
                "state",
                "disabled",
            ),
            "ocr": scheduler.health() if scheduler is not None else {"state": "idle"},
            "monitoring": self._is_monitoring(),
            "windows": [
                {
                    "window_title": context.get("window_title", ""),
                    "system_name": context.get("system_name", "Unknown"),
                    "runtime_status": context.get("runtime_status", ""),
                    "region": context.get("region"),
                    "last_error": context.get("last_error", ""),
                }
                for context in self._worker_contexts.values()
            ],
        }
        try:
            bundle = export_diagnostic_bundle(
                Path(target),
                diagnostics,
                default_log_path(),
            )
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self._log_message(f"诊断包已导出：{bundle}")

    def _begin_identity_check(self, action: str = "runtime") -> None:
        """Validate a configured API key or continue without authentication."""
        if action == "monitor":
            self._identity_wants_monitor = True
        elif action == "alert":
            self._identity_wants_alert = True
        if _instance_attr(self, "_identity_check_running", False):
            return

        api_key = self._settings.get_api_key()
        if _instance_attr(self, "_intel_url", None) == "":
            self._api_key_validated = False
            self._identity_wants_monitor = False
            self._identity_wants_alert = False
            self._settings.set_auth_status("未配置服务端")
            if action == "monitor":
                self._start_monitor(identity_checked=True)
            elif action == "alert":
                self._alert_btn.setChecked(False)
            return
        if not api_key:
            self._api_key_validated = False
            self._identity_wants_monitor = False
            self._identity_wants_alert = False
            self._settings.set_auth_status("未启用认证")
            if action == "monitor":
                self._start_monitor(identity_checked=True)
            elif action == "alert":
                self._start_alert(identity_checked=True)
            return
        if self._intel_client is None:
            self._settings.set_auth_status("认证客户端不可用，请检查服务端地址", error=True)
            if action == "monitor":
                self._monitor_btn.setChecked(False)
                self._monitor_start_state = "failed"
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
        state_store = self._settings.auth_state_store()
        state = state_store.load()
        resolved_names = {
            str(item.get("character_name") or "").strip().casefold()
            for item in state.get("character_identities", [])
            if isinstance(item, dict)
        }
        names_to_resolve = list(pending_names)
        names_to_resolve_keys = {item.casefold() for item in names_to_resolve}
        names_to_resolve.extend(
            name
            for name in state.get("characters", [])
            if str(name).strip().casefold() not in resolved_names
            and str(name).strip().casefold()
            not in names_to_resolve_keys
        )
        if names_to_resolve:
            ensure = getattr(client, "ensure_eve_character_check", None)
            identity = (
                ensure(
                    names_to_resolve,
                    client_id=str(_instance_attr(self, "_heartbeat_client_id", "")),
                )
                if callable(ensure)
                else client.verify_eve_characters(names_to_resolve)
            )
            if bool(identity.get("verified")):
                self._identity_scanner.mark_verified(names_to_resolve)
                remember = getattr(state_store, "remember_character_identities", None)
                if callable(remember):
                    remember(identity.get("characters", []))
            elif not bool(identity.get("pending")):
                raise IntelApiError(
                    str(identity.get("reason") or "EVE identity verification failed")
                )
        else:
            identity = {"verified": scan.identity_verified, "permanent": True}
        state = state_store.load()
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
            self._monitor_start_state = "failed"
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
        self._monitor_start_state = "idle"
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
            elif kind == "local_system":
                self._handle_local_system_error(exc, metadata)
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
            elif kind == "local_system":
                self._handle_local_system_result(result, metadata)
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

    def _handle_local_system_error(self, exc: Exception, metadata: dict) -> None:
        key = str(metadata.get("cache_key") or "")
        pending = _instance_attr(self, "_local_system_pending", set())
        pending.discard(key)
        self._local_system_pending = pending
        message = str(exc)
        if message != self._last_local_system_error:
            self._last_local_system_error = message
            self._log_message(f"Local chatlog system sync unavailable: {message}")

    def _handle_local_system_result(self, result: object, metadata: dict) -> None:
        cache_key = str(metadata.get("cache_key") or "")
        pending = _instance_attr(self, "_local_system_pending", set())
        pending.discard(cache_key)
        self._local_system_pending = pending
        detection = result
        cache = _instance_attr(self, "_local_system_cache", {})
        cache[cache_key] = (
            time.monotonic() + self._location_refresh_ttl,
            detection,
        )
        self._local_system_cache = cache
        if detection is None:
            return
        context = metadata.get("context")
        if isinstance(context, dict):
            self._apply_local_system_detection(context, detection)
        character_key = str(metadata.get("character_name") or "").strip().casefold()
        for collection_name in ("_detected_window_contexts", "_worker_contexts"):
            collection = _instance_attr(self, collection_name, {})
            for candidate in collection.values():
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("character_name") or "").strip().casefold() == character_key:
                    self._apply_local_system_detection(candidate, detection)
        if context is None:
            self._apply_local_system_detection(self, detection)
        self._last_local_system_error = ""
        controller = _instance_attr(self, "_alert_controller")
        if controller is not None:
            show_systems = getattr(controller, "show_monitoring_systems", None)
            if callable(show_systems):
                show_systems(self._monitoring_system_names())
        for method_name in (
            "_refresh_monitor_window_action_labels",
            "_refresh_window_status_table",
            "_refresh_status_cards",
        ):
            method = getattr(self, method_name, None)
            if callable(method):
                method()

    def _apply_local_system_detection(self, target, detection: object) -> None:
        system_name = str(getattr(detection, "system_name", "") or "").strip()
        if not system_name:
            return
        if isinstance(target, dict):
            previous = str(target.get("system_name") or "Unknown")
            target["system_name"] = system_name
            target["system_id"] = None
            target["system_source"] = "chatlog"
            if previous != system_name:
                self._log_message(
                    f"{target.get('window_title') or target.get('character_name') or 'EVE'}: "
                    f"Current system from local chatlog: {system_name}"
                )
            return
        previous = str(_instance_attr(self, "_intel_system", "Unknown"))
        self._intel_system = system_name
        self._intel_system_id = None
        self._intel_system_source = "chatlog"
        if previous != system_name:
            self._heartbeat_last_action = "local_system_sync"
            self._heartbeat_last_success_at = heartbeat_now_iso()

    def _start_monitor(self, *, identity_checked: bool = False) -> None:
        start_state = _instance_attr(self, "_monitor_start_state", "idle")
        if not identity_checked:
            if start_state != "idle":
                return
            self._monitor_start_state = "awaiting_identity"
        elif start_state in {"starting", "failed"}:
            return
        else:
            self._monitor_start_state = "starting"
        self._monitor_reconnect_scheduled = False
        if not identity_checked:
            self._begin_identity_check("monitor")
            return
        targets = self._build_monitor_targets()
        if not targets:
            self._detect_window()
            targets = self._build_monitor_targets()

        missing_regions = [
            target
            for target in targets
            if not isinstance(target.get("region"), dict)
        ]
        if missing_regions:
            QMessageBox.critical(
                self,
                "错误",
                "已选择监控窗口，但尚未选择监控区域，请先点击“选择区域”。",
            )
            self._monitor_btn.setChecked(False)
            self._monitor_start_state = "failed"
            return

        if not targets:
            actions = _instance_attr(self, "_monitor_window_actions")
            no_selection = bool(actions) and not any(
                action.isChecked() for action in actions.values()
            )
            QMessageBox.critical(
                self,
                "错误",
                (
                    "尚未选择监控窗口，请打开“监控窗口”菜单进行勾选。"
                    if no_selection
                    else "当前没有可用的 EVE 窗口。"
                ),
            )
            self._monitor_btn.setChecked(False)
            self._monitor_start_state = "failed"
            return

        existing_workers = list(_instance_attr(self, "_workers", {}).values())
        existing_workers.extend(
            worker
            for worker in _instance_attr(self, "_stopping_monitor_workers", set())
            if worker not in existing_workers
        )
        if existing_workers:
            self._monitor_restart_pending = True
            self._status_label.setText("正在重建监控")
            self._status_label.setStyleSheet(
                "color: #f0b35a; font-weight: bold;"
            )
            self._stop_monitor_workers(timeout_ms=0)
            self._monitor_start_state = "idle"
            return

        for target in targets:
            self._refresh_intel_location(force=True, context=target)
        primary_target = targets[0]
        self._intel_system = str(primary_target.get("system_name") or "Unknown")
        self._intel_system_id = primary_target.get("system_id")
        self._intel_system_source = str(
            primary_target.get("system_source") or "default"
        )

        self._workers = {}
        self._worker_contexts = {}
        interval = self._settings.get_interval()
        ocr_engine = _instance_attr(self, "_ocr")
        if ocr_engine is None:
            self._ocr_scheduler = SharedOCRScheduler()
            self._ocr_scheduler.warm_up()
            ocr_engine = self._ocr_scheduler
        for index, target in enumerate(targets):
            worker = MonitorWorker(
                Capturer(),
                ocr_engine,
            )
            window = target["window"]
            region = target["region"]
            worker.set_window(window)
            worker.set_region(region["x"], region["y"], region["w"], region["h"])
            worker.set_interval(interval)
            set_scan_offset = getattr(worker, "set_scan_offset", None)
            if callable(set_scan_offset):
                set_scan_offset(index * interval / max(1, len(targets)))
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
        self._monitor_start_state = "idle"

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
        self._stop_monitor_workers(
            timeout_ms=None if wait_for_workers else 0,
        )
        self._monitor_btn.setText("开始监控")
        self._monitor_btn.setStyleSheet(monitor_button_style(active=False))
        if _instance_attr(self, "_stopping_monitor_workers", set()):
            self._status_label.setText("正在停止")
            self._status_label.setStyleSheet(
                "color: #f0b35a; font-weight: bold;"
            )
            self._log_message("正在停止监控线程...")
        else:
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

        scheduler = _instance_attr(self, "_ocr_scheduler")
        if scheduler is not None:
            scheduler.close(wait=False)
            self._ocr_scheduler = None

        if timeout_ms == 0:
            for worker in workers:
                self._disconnect_worker_signals(worker)
            self._stopping_monitor_workers = set(workers)
            self._workers = {}
            self._worker_contexts = {}
            self._worker = None
            self._monitor_btn.setEnabled(False)
            self._refresh_window_status_table()
            self._refresh_monitor_window_action_labels()
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
        self._workers = {}
        self._worker_contexts = {}
        self._worker = None
        self._refresh_window_status_table()
        self._refresh_monitor_window_action_labels()
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
        restart_pending = bool(
            _instance_attr(self, "_monitor_restart_pending", False)
        )
        self._monitor_restart_pending = False
        if restart_pending and self._monitor_btn.isChecked():
            self._start_monitor(identity_checked=True)
            return
        if not _instance_attr(self, "_shutdown_in_progress", False):
            set_text = getattr(self._monitor_btn, "setText", None)
            if callable(set_text):
                set_text("开始监控")
            set_style = getattr(self._monitor_btn, "setStyleSheet", None)
            if callable(set_style):
                set_style(monitor_button_style(active=False))
            status_label = _instance_attr(self, "_status_label")
            set_status = getattr(status_label, "setText", None)
            if callable(set_status):
                set_status("已停止")
            set_status_style = getattr(status_label, "setStyleSheet", None)
            if callable(set_status_style):
                set_status_style("color: #888;")
            self._log_message("监控线程已停止")

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
            return PersistentIntelApiClient(
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
        upload_manager = _instance_attr(self, "_upload_manager")
        if upload_manager is not None:
            upload_manager.submit_snapshot(
                client_id,
                payload,
                {
                    "kind": "ocr",
                    "context": context,
                    "names": list(names),
                },
            )
            return
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
        if self._intel_client is None:
            return
        monitoring = (
            self._is_monitoring()
            if monitoring_override is None
            else bool(monitoring_override)
        )
        contexts = list(getattr(self, "_worker_contexts", {}).values())
        context_errors = [
            f"{str(context.get('character_name') or context.get('window_title') or 'EVE')}: "
            f"{str(context.get('last_error') or '').strip()}"
            for context in contexts
            if str(context.get("last_error") or "").strip()
        ]
        transport_error = str(
            _instance_attr(self, "_last_heartbeat_error", "") or ""
        ).strip()
        reported_errors = list(context_errors)
        if transport_error:
            reported_errors.append(f"心跳连接: {transport_error}")
        heartbeat_error = "；".join(dict.fromkeys(reported_errors))
        if not heartbeat_error:
            heartbeat_error = str(self._heartbeat_last_error or "").strip()
        details = build_detector_heartbeat_details(
            monitoring=monitoring,
            system_name=self._intel_system,
            system_source=self._intel_system_source,
            popup_alerts=self._popup_alerts_enabled,
            window_title=self._window_combo.currentText(),
            last_action=self._heartbeat_last_action,
            last_error=heartbeat_error,
            client_version=self._heartbeat_runtime["client_version"],
            host=self._heartbeat_runtime["host"],
            last_success_at=self._heartbeat_last_success_at,
        )
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
                    **(
                        {"runtime_status": str(context.get("runtime_status") or "")}
                        if str(context.get("runtime_status") or "").strip()
                        else {}
                    ),
                    **(
                        {"last_error": str(context.get("last_error") or "")}
                        if str(context.get("last_error") or "").strip()
                        else {}
                    ),
                }
                for context in contexts
            ]
            details["target_count"] = len(contexts)
        payload = {
            "client_id": self._heartbeat_client_id,
            "client_type": "detector_client",
            "label": "Detector Client",
            "status": "error" if heartbeat_error else ("running" if monitoring else "idle"),
            "heartbeat_interval_seconds": self._heartbeat_interval,
            "details": details,
        }
        upload_manager = _instance_attr(self, "_upload_manager")
        if upload_manager is not None:
            upload_manager.submit_heartbeat(
                payload,
                {"kind": "heartbeat", "task_key": task_key},
            )
            return
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

    def _on_upload_state_changed(self, state: str, label: str) -> None:
        """Expose reliable-upload state without overloading monitor status."""
        connection_label = _instance_attr(self, "_connection_label")
        if connection_label is not None:
            colors = {
                "online": "#37d6b0",
                "reconnecting": "#f6c760",
                "offline_cached": "#f6c760",
                "authentication_failed": "#ff6b73",
            }
            connection_label.setText(label)
            connection_label.setStyleSheet(
                f"color: {colors.get(state, '#a8b1c7')}; font-weight: 600;"
            )
        if state == "authentication_failed":
            self._disable_authenticated_features(label)
        elif state in {"reconnecting", "offline_cached"}:
            self._last_heartbeat_error = label
        else:
            self._last_heartbeat_error = ""

    def _reset_upload_manager(self) -> None:
        """Rebuild reliable uploads after server URL or credentials change."""
        previous = _instance_attr(self, "_upload_manager")
        if previous is not None:
            previous.shutdown(timeout=0.0)
        self._upload_manager = None
        client = _instance_attr(self, "_intel_client")
        if client is None or not callable(getattr(client, "post_ocr_snapshot", None)):
            return
        try:
            self._upload_manager = ReliableUploadManager(client, parent=self)
        except RuntimeError:
            # Some focused tests construct MainWindow without QObject.__init__.
            self._upload_manager = ReliableUploadManager(client, parent=None)
        self._upload_manager.state_changed.connect(self._on_upload_state_changed)
        self._upload_manager.snapshot_uploaded.connect(
            self._handle_ocr_publish_success
        )
        self._upload_manager.heartbeat_uploaded.connect(
            self._on_reliable_heartbeat_uploaded
        )
        self._set_heartbeat_enabled(True)
        QTimer.singleShot(0, self._publish_heartbeat)

    def _on_reliable_heartbeat_uploaded(self, _metadata: object) -> None:
        self._last_heartbeat_error = ""
        self._heartbeat_last_success_at = heartbeat_now_iso()
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

        resolved = (
            self._refresh_local_system_from_chatlog(context=context)
            or has_cached_location
        )
        if context is not None:
            self._remember_monitor_window_context(context)
            self._refresh_monitor_window_action_labels()
        return resolved

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
        cache_key = character_name.casefold() or "*"
        cache = _instance_attr(self, "_local_system_cache", {})
        cached = cache.get(cache_key)
        if cached is not None:
            expires_at, detection = cached
            if time.monotonic() < float(expires_at):
                if detection is not None:
                    self._apply_local_system_detection(
                        context if context is not None else self,
                        detection,
                    )
                return detection is not None

        runner = _instance_attr(self, "_network_tasks")
        if runner is not None:
            pending = _instance_attr(self, "_local_system_pending", set())
            if cache_key not in pending:
                pending.add(cache_key)
                self._local_system_pending = pending
                log_dir = settings.get_channel_log_dir()
                runner.submit_once(
                    f"local-system:{cache_key}",
                    lambda log_dir=log_dir, character_name=character_name: find_latest_local_system(
                        log_dir,
                        character_name=character_name,
                    ),
                    {
                        "kind": "local_system",
                        "cache_key": cache_key,
                        "character_name": character_name,
                        "context": context,
                    },
                )
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
            controller = _instance_attr(self, "_alert_controller")
            if controller is not None:
                controller.show_monitoring_systems(
                    self._monitoring_system_names()
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

        from PyQt6.QtWidgets import QMenu

        self._tray_menu = QMenu(self)

        show_action = self._tray_menu.addAction("显示主窗口")
        show_action.triggered.connect(self.show)

        self._tray_menu.addSeparator()
        self._tray_behavior_actions = {}
        behavior_items = (
            ("start_with_windows", "开机启动", self._settings.get_start_with_windows),
            ("start_minimized", "启动后最小化", self._settings.get_start_minimized),
            ("close_to_tray", "关闭到托盘", self._settings.get_close_to_tray),
            (
                "restore_monitor_state",
                "恢复上次监控状态",
                self._settings.get_restore_monitor_state,
            ),
        )
        for preference, label, getter in behavior_items:
            action = self._tray_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(getter())
            action.toggled.connect(
                lambda checked, name=preference: self._settings.set_behavior_preference(
                    name,
                    checked,
                )
            )
            self._tray_behavior_actions[preference] = action
        self._tray_menu.aboutToShow.connect(self._sync_tray_behavior_actions)

        self._tray_menu.addSeparator()
        quit_action = self._tray_menu.addAction("退出")
        quit_action.triggered.connect(self._quit_app)

        self._tray.show()

    def _sync_tray_behavior_actions(self) -> None:
        values = {
            "start_with_windows": self._settings.get_start_with_windows(),
            "start_minimized": self._settings.get_start_minimized(),
            "close_to_tray": self._settings.get_close_to_tray(),
            "restore_monitor_state": self._settings.get_restore_monitor_state(),
        }
        for name, action in self._tray_behavior_actions.items():
            previous = action.blockSignals(True)
            action.setChecked(values[name])
            action.blockSignals(previous)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Context:
            menu = getattr(self, "_tray_menu", None)
            if menu is not None:
                menu.exec(QCursor.pos())
            return
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()

    def closeEvent(self, event):
        """Hide immediately while background workers unwind asynchronously."""
        runtime_settings = _instance_attr(self, "_runtime_settings")
        if runtime_settings is not None:
            runtime_settings.setValue("window/geometry", self.saveGeometry())
            runtime_settings.setValue("monitor/was_running", self._is_monitoring())
        settings = _instance_attr(self, "_settings")
        close_to_tray = bool(
            settings is not None
            and getattr(settings, "get_close_to_tray", lambda: False)()
        )
        updater = _instance_attr(self, "_updater")
        update_ready = bool(
            updater is not None
            and getattr(updater, "ready_to_install", False)
        )
        if (
            close_to_tray
            and not update_ready
            and not _instance_attr(self, "_shutdown_in_progress", False)
        ):
            self.hide()
            tray = _instance_attr(self, "_tray")
            if tray is not None:
                tray.showMessage("EVE Sentry", "客户端仍在托盘中运行")
            event.ignore()
            return
        self._quit_app()
        event.ignore()

    def _quit_app(self):
        if _instance_attr(self, "_shutdown_in_progress", False):
            return
        updater = _instance_attr(self, "_updater")
        if (
            updater is not None
            and getattr(updater, "ready_to_install", False)
            and not updater.install_on_exit()
        ):
            self.show()
            self.raise_()
            return
        self._shutdown_in_progress = True
        shutdown_timeout = _env_float(
            "EVE_SENTRY_SHUTDOWN_TIMEOUT",
            default=10.0,
            minimum=2.0,
        )
        self._shutdown_deadline = time.monotonic() + shutdown_timeout
        if isinstance(self, MainWindow):
            completed = threading.Event()
            self._shutdown_complete = completed
            threading.Thread(
                target=_force_exit_if_shutdown_stalls,
                args=(completed, shutdown_timeout + 0.5),
                name="eve-sentry-shutdown-watchdog",
                daemon=True,
            ).start()
        runtime_settings = _instance_attr(self, "_runtime_settings")
        if runtime_settings is not None:
            runtime_settings.setValue("window/geometry", self.saveGeometry())
            runtime_settings.setValue("monitor/was_running", self._is_monitoring())
        self.hide()
        tray = _instance_attr(self, "_tray")
        if tray is not None:
            tray.hide()
        MainWindow._set_heartbeat_enabled(self, False)
        self._stop_monitor(wait_for_workers=False)
        self._stop_alert(wait_for_worker=False)
        network_tasks = _instance_attr(self, "_network_tasks")
        if network_tasks is not None:
            network_tasks.shutdown()
        upload_manager = _instance_attr(self, "_upload_manager")
        if upload_manager is not None:
            upload_manager.shutdown(timeout=0.0)
            self._upload_manager = None
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
        preview_worker = _instance_attr(self, "_preview_capture_worker")
        preview_running = (
            preview_worker is not None and preview_worker.isRunning()
        )
        updater = _instance_attr(self, "_updater")
        updater_running = bool(
            updater is not None
            and getattr(updater, "has_running_file_tasks", False)
        )
        if monitor_running or alert_running or preview_running or updater_running:
            deadline = float(
                _instance_attr(self, "_shutdown_deadline", float("inf"))
            )
            if time.monotonic() >= deadline:
                logger.error(
                    "Shutdown deadline reached: monitor=%s alert=%s preview=%s updater=%s",
                    monitor_running,
                    alert_running,
                    preview_running,
                    updater_running,
                )
                QApplication.quit()
                return
            QTimer.singleShot(50, self._finish_quit_when_workers_stop)
            return
        completed = _instance_attr(self, "_shutdown_complete")
        if completed is not None:
            completed.set()
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


def _string_list(value) -> list[str]:
    """Normalize a QSettings value to a clean string list."""
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    return [str(item).strip() for item in values if str(item).strip()]


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


def _titled_windows(windows: list[dict]) -> list[dict]:
    """Exclude process-owned helper windows that cannot identify an account."""
    return [
        window
        for window in windows
        if str(window.get("title") or "").strip()
    ]


def _window_geometry_signature(window: dict) -> tuple:
    """Return the geometry fields that require a live capture-region remap."""
    return (
        int(window.get("x") or 0),
        int(window.get("y") or 0),
        int(window.get("w") or 0),
        int(window.get("h") or 0),
        str(window.get("monitor") or ""),
    )
