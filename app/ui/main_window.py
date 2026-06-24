"""Main application window."""

import logging
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
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
from app.models.whitelist import Whitelist
from app.ui.alert_dialog import AlertDialog
from app.ui.settings import SettingsPanel

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window: settings on the left, log on the right, tray icon."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Sentry")
        self.setMinimumSize(700, 450)

        # ---- Models & Engine ----
        self._whitelist = Whitelist("whitelist.json")
        self._capturer = Capturer()
        self._ocr = OCREngine(lang="ch", confidence_threshold=0.7)
        self._detector = Detector(self._whitelist, cooldown_seconds=60.0)
        self._worker: MonitorWorker | None = None

        # ---- Alert dialog state ----
        self._alert_visible = False
        self._alert_queue: list[list[str]] = []

        # ---- Manually selected region override ----
        self._manual_region: dict | None = None

        # ---- Central widget ----
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Left: settings panel
        self._settings = SettingsPanel(self._whitelist)
        self._settings.setFixedWidth(220)
        root.addWidget(self._settings)

        # Right: log area + control buttons
        right = QVBoxLayout()
        right.setSpacing(6)

        # Monitor button
        self._monitor_btn = QPushButton("开始监控")
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

        # Window info row
        self._window_label = QLabel("窗口: 未检测")
        self._window_label.setStyleSheet("color: #666; font-size: 11px;")
        right.addWidget(self._window_label)

        right.addWidget(QLabel("状态日志:"))

        # Log text area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            "QTextEdit { background: #1a1a2e; color: #e0e0e0; "
            "font-family: Consolas, monospace; font-size: 12px; }"
        )
        right.addWidget(self._log)

        # Bottom buttons
        btn_row = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self._log.clear)
        btn_row.addWidget(clear_btn)

        select_btn = QPushButton("重选区域")
        select_btn.clicked.connect(self._select_region)
        btn_row.addWidget(select_btn)

        btn_row.addStretch()
        right.addLayout(btn_row)

        root.addLayout(right, 1)

        # ---- Status bar ----
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status_label = QLabel("● 未启动")
        self._status.addWidget(self._status_label)

        # ---- System tray ----
        self._setup_tray()

        # Try auto-detect window
        self._detect_window()

    # ------------------------------------------------------------------
    # Window detection
    # ------------------------------------------------------------------

    def _detect_window(self) -> None:
        """Try to find the EVE window and display info."""
        keyword = self._settings.get_keyword()
        info = self._capturer.find_eve_window(keyword=keyword)
        if info:
            self._window_label.setText(
                f"窗口: {info['title']} ({info['w']}×{info['h']})"
            )
            self._detected_region = info
        else:
            self._window_label.setText("窗口: 未找到 (请手动框选)")

    # ------------------------------------------------------------------
    # Region selection (manual fallback)
    # ------------------------------------------------------------------

    def _select_region(self) -> None:
        """Show a dialog for manual coordinate entry.

        Pre-fills with auto-detected window values if available;
        otherwise uses a reasonable default.
        """
        self._detect_window()
        defaults = (
            self._detected_region
            if hasattr(self, "_detected_region") and self._detected_region
            else {"x": 0, "y": 0, "w": 800, "h": 600}
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("手动选择截图区域")
        form = QFormLayout(dlg)

        x_spin = QSpinBox()
        x_spin.setRange(0, 99999)
        x_spin.setValue(defaults["x"])
        form.addRow("X 坐标:", x_spin)

        y_spin = QSpinBox()
        y_spin.setRange(0, 99999)
        y_spin.setValue(defaults["y"])
        form.addRow("Y 坐标:", y_spin)

        w_spin = QSpinBox()
        w_spin.setRange(1, 99999)
        w_spin.setValue(defaults["w"])
        form.addRow("宽度:", w_spin)

        h_spin = QSpinBox()
        h_spin.setRange(1, 99999)
        h_spin.setValue(defaults["h"])
        form.addRow("高度:", h_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._manual_region = {
                "x": x_spin.value(),
                "y": y_spin.value(),
                "w": w_spin.value(),
                "h": h_spin.value(),
            }
            self._log_message(
                f"手动区域已设置: ({self._manual_region['x']}, "
                f"{self._manual_region['y']}) "
                f"{self._manual_region['w']}×{self._manual_region['h']}"
            )

    # ------------------------------------------------------------------
    # Monitor start / stop
    # ------------------------------------------------------------------

    def _toggle_monitor(self, checked: bool) -> None:
        if checked:
            self._start_monitor()
        else:
            self._stop_monitor()

    def _start_monitor(self) -> None:
        # Ensure we have a region — prefer manual override, fall back to auto-detect
        if not hasattr(self, "_detected_region") or self._detected_region is None:
            self._detect_window()
        region = self._manual_region or (
            self._detected_region
            if hasattr(self, "_detected_region")
            else None
        )
        if region is None:
            QMessageBox.critical(self, "错误", "找不到 EVE 窗口，请确保游戏已运行。")
            self._monitor_btn.setChecked(False)
            return

        # Guard: if an old worker thread is still alive, try to stop it
        if self._worker is not None:
            if self._worker.isRunning():
                self._log_message("正在停止旧的监控线程...")
                self._worker.stop()
                if not self._worker.wait(5000):
                    logger.warning(
                        "Old worker thread did not stop within 5 s — rejecting start"
                    )
                    self._monitor_btn.setChecked(False)
                    QMessageBox.critical(
                        self, "错误", "无法停止旧的监控线程，请重启应用。"
                    )
                    return
                self._log_message("旧线程已停止")
            self._disconnect_worker_signals()

        r = region
        self._worker = MonitorWorker(self._capturer, self._ocr, self._detector)
        self._worker.set_region(r["x"], r["y"], r["w"], r["h"])
        self._worker.set_interval(self._settings.get_interval())

        self._worker.threat_detected.connect(self._on_threat)
        self._worker.status_update.connect(self._log_message)
        self._worker.scan_complete.connect(self._update_scan_count)

        self._worker.start()

        self._monitor_btn.setText("停止监控")
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #cc0000; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #ee2222; }"
        )
        self._status_label.setText("● 运行中")
        self._status_label.setStyleSheet("color: #228b22; font-weight: bold;")
        self._log_message("监控已启动")

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
                logger.warning(
                    "Worker thread did not stop within 3 s timeout — "
                    "disconnecting signals and creating fresh engine instances"
                )
                self._disconnect_worker_signals()
                # Old thread is still alive holding PaddleOCR — create fresh
                # instances so the next start is safe
                self._capturer = Capturer()
                self._ocr = OCREngine(lang="ch", confidence_threshold=0.7)
                self._detector = Detector(
                    self._whitelist, cooldown_seconds=60.0
                )
            self._worker = None

        self._monitor_btn.setText("开始监控")
        self._monitor_btn.setStyleSheet(
            "QPushButton { background: #228b22; color: white; border-radius: 4px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #2ea62e; }"
        )
        self._status_label.setText("● 已停止")
        self._status_label.setStyleSheet("color: #888;")
        self._log_message("监控已停止")

    # ------------------------------------------------------------------
    # Alert handling
    # ------------------------------------------------------------------

    def _on_threat(self, threats: list[str]) -> None:
        """Show non-blocking alert dialog when threats are detected.

        If an alert is already visible, queue the threat names so they
        are shown once the current dialog closes — this prevents cascading
        modal dialogs from blocking the main thread.
        """
        if self._alert_visible:
            self._alert_queue.append(threats)
            return

        self._alert_visible = True
        dlg = AlertDialog(threats, self)
        dlg.finished.connect(self._on_alert_closed)
        dlg.show()

    def _on_alert_closed(self) -> None:
        """Called when the alert dialog is dismissed."""
        self._alert_visible = False
        if self._alert_queue:
            next_threats = self._alert_queue.pop(0)
            self._on_threat(next_threats)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_message(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {msg}")

    def _update_scan_count(self, count: int) -> None:
        # Status bar already shows "running" — we just note it
        pass

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        # icon_path = Path(__file__).parent.parent.parent / "resources" / "icon.ico"
        # if icon_path.exists():
        #     self._tray.setIcon(QIcon(str(icon_path.resolve())))
        self._tray.setToolTip("EVE Sentry")
        self._tray.activated.connect(self._on_tray_activated)

        menu = self._tray.contextMenu()
        if menu is None:
            from PyQt6.QtWidgets import QMenu
            menu = QMenu()
            self._tray.setContextMenu(menu)

        show_action = QAction("显示主窗口")
        show_action.triggered.connect(self.show)
        menu.addAction(show_action)

        quit_action = QAction("退出")
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
