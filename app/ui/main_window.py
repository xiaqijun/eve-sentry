"""Main application window."""

import logging
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication,
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
        """Pop up a full-screen overlay for drag-to-select."""
        # This is a simplified version — the real overlay would be a
        # transparent fullscreen widget.  For now, fall back to the
        # detected window and show a message.
        self._detect_window()
        if hasattr(self, "_detected_region"):
            info = self._detected_region
            self._log_message(f"使用窗口区域: {info['w']}×{info['h']}")
        else:
            QMessageBox.warning(
                self,
                "手动框选",
                "请确保 EVE 已运行，或手动输入窗口关键词后再试。",
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
        # Ensure we have a region
        if not hasattr(self, "_detected_region") or self._detected_region is None:
            self._detect_window()
        if not hasattr(self, "_detected_region") or self._detected_region is None:
            QMessageBox.critical(self, "错误", "找不到 EVE 窗口，请确保游戏已运行。")
            self._monitor_btn.setChecked(False)
            return

        r = self._detected_region
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

    def _stop_monitor(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)
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
        """Show alert dialog when threats are detected."""
        dlg = AlertDialog(threats, self)
        dlg.exec()

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
