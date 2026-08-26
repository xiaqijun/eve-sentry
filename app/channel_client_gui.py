"""Standalone desktop client for forwarding selected EVE intel chatlogs."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.channels.identity_logs import ClientAuthStateStore
from app.channels.log_watcher import (
    ChatLogWatcher,
    channel_name_from_path,
    resolve_chatlog_dir,
)
from app.core.client_identity import persistent_client_id
from app.core.heartbeat import (
    build_channel_heartbeat_details,
    heartbeat_now_iso,
    resolve_runtime_identity,
    summarize_heartbeat_error,
)
from app.intel_client import IntelApiClient, IntelApiError, is_valid_api_key
from app.ui.settings import normalize_server_url, server_url_validation_error
from app.ui.theme import APP_QSS, monitor_button_style, status_card_style
from app.version import current_version


logger = logging.getLogger(__name__)
MAX_EVENT_ROWS = 300


def _local_state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "EVE Sentry"
    return Path.home() / ".eve-sentry"


def default_channel_client_config_path() -> Path:
    """Return the standalone channel client's settings path."""
    return _local_state_root() / "channel_client_settings.json"


def default_channel_client_offset_path() -> Path:
    """Return the standalone channel client's durable offset path."""
    return _local_state_root() / "channel_client_offsets.json"


def default_channel_client_auth_path() -> Path:
    """Return the standalone channel client's protected credential path."""
    return _local_state_root() / "channel_client_auth.json"


@dataclass
class ChannelClientConfig:
    """User-editable settings that do not contain credential secrets."""

    server_url: str = ""
    log_dir: str = ""
    selected_channels: list[str] = field(default_factory=list)
    scan_interval_seconds: int = 1
    ignore_existing_files: bool = True

    def normalized(self) -> "ChannelClientConfig":
        log_dir = self.log_dir.strip() or str(resolve_chatlog_dir())
        channels = sorted(
            {str(item).strip() for item in self.selected_channels if str(item).strip()},
            key=str.casefold,
        )
        return ChannelClientConfig(
            server_url=normalize_server_url(self.server_url),
            log_dir=log_dir,
            selected_channels=channels,
            scan_interval_seconds=min(10, max(1, int(self.scan_interval_seconds))),
            ignore_existing_files=bool(self.ignore_existing_files),
        )


def load_channel_client_config(
    path: str | Path | None = None,
) -> ChannelClientConfig:
    """Load settings defensively and fall back to safe defaults."""
    config_path = Path(path) if path else default_channel_client_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ChannelClientConfig(
        server_url=str(payload.get("server_url") or ""),
        log_dir=str(payload.get("log_dir") or resolve_chatlog_dir()),
        selected_channels=list(payload.get("selected_channels") or []),
        scan_interval_seconds=int(payload.get("scan_interval_seconds") or 1),
        ignore_existing_files=bool(payload.get("ignore_existing_files", True)),
    ).normalized()


def save_channel_client_config(
    config: ChannelClientConfig,
    path: str | Path | None = None,
) -> Path:
    """Persist settings atomically without storing the device key."""
    config_path = Path(path) if path else default_channel_client_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = config.normalized()
    temp_path = config_path.with_name(f".{config_path.name}.tmp")
    temp_path.write_text(
        json.dumps(asdict(normalized), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(config_path)
    return config_path


def discover_channel_names(log_dir: str | Path) -> list[str]:
    """Return stable channel names ordered by latest log activity."""
    root = Path(log_dir)
    if not root.is_dir():
        return []
    latest: dict[str, tuple[float, str]] = {}
    for path in root.glob("*.txt"):
        if not path.is_file():
            continue
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        name = channel_name_from_path(path).strip()
        if not name:
            continue
        key = name.casefold()
        current = latest.get(key)
        if current is None or modified_at > current[0]:
            latest[key] = (modified_at, name)
    return [item[1] for item in sorted(latest.values(), key=lambda item: (-item[0], item[1].casefold()))]


def create_channel_watcher(
    config: ChannelClientConfig,
    offset_path: str | Path,
) -> ChatLogWatcher:
    """Create a watcher that skips only the first baseline, not future rotations."""
    normalized = config.normalized()
    state_path = Path(offset_path)
    watcher = ChatLogWatcher(
        log_dir=normalized.log_dir,
        channels=normalized.selected_channels,
        state_path=state_path,
        start_at_end_for_new_files=False,
    )
    if normalized.ignore_existing_files and not state_path.exists():
        watcher.seed_to_end()
    return watcher


class ChannelClientWorker(QObject):
    """Tail selected chatlogs and forward complete lines outside the UI thread."""

    status_changed = pyqtSignal(object)
    line_processed = pyqtSignal(object)
    stopped = pyqtSignal()

    def __init__(
        self,
        config: ChannelClientConfig,
        api_key: str,
        offset_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.config = config.normalized()
        self.api_key = str(api_key or "").strip()
        self.offset_path = Path(offset_path) if offset_path else default_channel_client_offset_path()
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Request a cooperative stop from the GUI thread."""
        self._stop_event.set()

    def run(self) -> None:
        """Run until stopped while preserving offsets after successful uploads."""
        uploaded = 0
        last_success_at = ""
        last_error = ""
        last_heartbeat_at = 0.0
        runtime_identity = resolve_runtime_identity()
        client_id = persistent_client_id("channel")
        watcher = create_channel_watcher(self.config, self.offset_path)
        try:
            api = IntelApiClient(
                self.config.server_url,
                timeout=5.0,
                api_key=self.api_key,
            )
            if self.api_key:
                api.validate_api_key()
            self.status_changed.emit(
                {
                    "state": "running",
                    "uploaded": uploaded,
                    "last_success_at": last_success_at,
                    "last_error": "",
                }
            )
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= last_heartbeat_at:
                    try:
                        api.post_heartbeat(
                            client_id=client_id,
                            client_type="channel_client",
                            label="预警频道日志客户端",
                            heartbeat_interval_seconds=10.0,
                            details=build_channel_heartbeat_details(
                                server_parse=True,
                                last_action="watching",
                                last_error=last_error,
                                client_version=runtime_identity["client_version"],
                                host=runtime_identity["host"],
                                last_success_at=last_success_at,
                            ),
                        )
                    except IntelApiError as exc:
                        last_error = summarize_heartbeat_error(str(exc))
                        self._emit_status("warning", uploaded, last_success_at, last_error)
                    last_heartbeat_at = now + 10.0

                try:
                    pending_lines = watcher.poll_lines()
                except OSError as exc:
                    last_error = summarize_heartbeat_error(str(exc))
                    self._emit_status("warning", uploaded, last_success_at, last_error)
                    self._stop_event.wait(self.config.scan_interval_seconds)
                    continue

                for line in pending_lines:
                    if self._stop_event.is_set():
                        break
                    try:
                        result = api.post_channel_line(
                            line.text,
                            channel=line.channel,
                            defer_enrichment=True,
                        )
                    except IntelApiError as exc:
                        last_error = summarize_heartbeat_error(str(exc))
                        self._emit_status("warning", uploaded, last_success_at, last_error)
                        break
                    watcher.commit_line(line)
                    ignored = bool(result.get("ignored"))
                    if not ignored:
                        uploaded += 1
                    last_success_at = heartbeat_now_iso()
                    last_error = ""
                    self.line_processed.emit(
                        {
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "channel": line.channel,
                            "text": line.text,
                            "result": "已忽略" if ignored else "已上报",
                        }
                    )
                    self._emit_status("running", uploaded, last_success_at, "")

                self._stop_event.wait(self.config.scan_interval_seconds)
        except Exception as exc:
            logger.exception("Channel client worker stopped unexpectedly")
            self._emit_status(
                "error",
                uploaded,
                last_success_at,
                summarize_heartbeat_error(str(exc)),
            )
        finally:
            self.stopped.emit()

    def _emit_status(
        self,
        state: str,
        uploaded: int,
        last_success_at: str,
        last_error: str,
    ) -> None:
        self.status_changed.emit(
            {
                "state": state,
                "uploaded": uploaded,
                "last_success_at": last_success_at,
                "last_error": last_error,
            }
        )


class ChannelClientWindow(QMainWindow):
    """Configuration and live-status window for the channel client."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        super().__init__()
        self._config_path = Path(config_path) if config_path else default_channel_client_config_path()
        self._config = load_channel_client_config(self._config_path)
        self._auth_store = ClientAuthStateStore(default_channel_client_auth_path())
        self._worker: ChannelClientWorker | None = None
        self._thread: QThread | None = None
        self._status_cards: dict[str, tuple[QFrame, QLabel]] = {}

        self.setWindowTitle(f"EVE Sentry 预警频道日志客户端 {current_version()}")
        self.setMinimumSize(880, 650)
        self.resize(1020, 720)
        self.setStyleSheet(APP_QSS)
        self._build_ui()
        self._apply_config()
        self._refresh_channels()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("预警频道日志客户端")
        title.setObjectName("pageTitle")
        subtitle = QLabel("只读取已选择的 EVE 聊天日志，并交由服务端解析预警情报")
        subtitle.setObjectName("targetMeta")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_row.addLayout(title_block)
        title_row.addStretch()
        version = QLabel(f"v{current_version()}")
        version.setObjectName("brandMeta")
        title_row.addWidget(version)
        layout.addLayout(title_row)

        cards = QGridLayout()
        cards.setHorizontalSpacing(10)
        for index, (key, label, value) in enumerate(
            (
                ("state", "运行状态", "未启动"),
                ("channels", "已选频道", "0"),
                ("uploaded", "本次上报", "0"),
                ("success", "最近同步", "—"),
            )
        ):
            cards.addWidget(self._make_status_card(key, label, value), 0, index)
        layout.addLayout(cards)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_settings_panel(), 0)
        body.addWidget(self._build_event_panel(), 1)
        layout.addLayout(body, 1)

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("settingsPanel")
        panel.setFixedWidth(330)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        connection_group = QGroupBox("连接配置")
        connection_form = QFormLayout(connection_group)
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("https://example.com")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("可留空，或填写 eve_...")
        connection_form.addRow("服务端", self.server_edit)
        connection_form.addRow("设备密钥", self.key_edit)
        layout.addWidget(connection_group)

        log_group = QGroupBox("日志来源")
        log_layout = QVBoxLayout(log_group)
        path_row = QHBoxLayout()
        self.log_dir_edit = QLineEdit()
        self.log_dir_edit.setReadOnly(True)
        browse_button = QPushButton("选择")
        browse_button.clicked.connect(self._browse_log_dir)
        path_row.addWidget(self.log_dir_edit, 1)
        path_row.addWidget(browse_button)
        log_layout.addLayout(path_row)

        channel_actions = QHBoxLayout()
        channel_label = QLabel("选择需要解析的频道")
        channel_label.setObjectName("fieldLabel")
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._refresh_channels)
        channel_actions.addWidget(channel_label)
        channel_actions.addStretch()
        channel_actions.addWidget(refresh_button)
        log_layout.addLayout(channel_actions)
        self.channel_list = QListWidget()
        self.channel_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.channel_list.itemChanged.connect(self._update_channel_count)
        log_layout.addWidget(self.channel_list, 1)

        options = QHBoxLayout()
        options.addWidget(QLabel("扫描间隔"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 10)
        self.interval_spin.setSuffix(" 秒")
        options.addWidget(self.interval_spin)
        options.addStretch()
        log_layout.addLayout(options)
        self.ignore_existing_check = QCheckBox("首次启动忽略已有历史内容")
        self.ignore_existing_check.setToolTip("之后重新启动会从已保存的断点继续，不会漏掉离线期间追加的行")
        log_layout.addWidget(self.ignore_existing_check)
        layout.addWidget(log_group, 1)

        action_row = QHBoxLayout()
        self.start_button = QPushButton("开始监听")
        self.start_button.setStyleSheet(monitor_button_style(active=False))
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        action_row.addWidget(self.start_button, 1)
        action_row.addWidget(self.stop_button)
        layout.addLayout(action_row)
        return panel

    def _build_event_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("workspace")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        title_row = QHBoxLayout()
        title = QLabel("最近解析记录")
        title.setObjectName("sectionTitle")
        open_button = QPushButton("打开日志目录")
        open_button.clicked.connect(self._open_log_dir)
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(open_button)
        layout.addLayout(title_row)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.event_table = QTableWidget(0, 4)
        self.event_table.setHorizontalHeaderLabels(("时间", "频道", "结果", "日志内容"))
        self.event_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.event_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.event_table.setAlternatingRowColors(True)
        self.event_table.verticalHeader().setVisible(False)
        header = self.event_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.event_table, 1)
        return panel

    def _make_status_card(self, key: str, title: str, value: str) -> QFrame:
        card = QFrame()
        card.setObjectName("statusCard")
        card.setStyleSheet(status_card_style("idle"))
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 8, 12, 8)
        title_label = QLabel(title)
        title_label.setObjectName("statusCardTitle")
        value_label = QLabel(value)
        value_label.setObjectName("statusCardValue")
        card_layout.addWidget(title_label)
        card_layout.addWidget(value_label)
        self._status_cards[key] = (card, value_label)
        return card

    def _apply_config(self) -> None:
        self.server_edit.setText(self._config.server_url)
        self.key_edit.setText(self._auth_store.api_key())
        self.log_dir_edit.setText(self._config.log_dir)
        self.interval_spin.setValue(self._config.scan_interval_seconds)
        self.ignore_existing_check.setChecked(self._config.ignore_existing_files)

    def _selected_channels(self) -> list[str]:
        result: list[str] = []
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result

    def _current_config(self) -> ChannelClientConfig:
        return ChannelClientConfig(
            server_url=self.server_edit.text(),
            log_dir=self.log_dir_edit.text(),
            selected_channels=self._selected_channels(),
            scan_interval_seconds=self.interval_spin.value(),
            ignore_existing_files=self.ignore_existing_check.isChecked(),
        ).normalized()

    def _refresh_channels(self) -> None:
        selected = set(self._selected_channels() or self._config.selected_channels)
        names = discover_channel_names(self.log_dir_edit.text())
        self.channel_list.blockSignals(True)
        self.channel_list.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked
            )
            self.channel_list.addItem(item)
        self.channel_list.blockSignals(False)
        self._update_channel_count()

    def _update_channel_count(self, _item: QListWidgetItem | None = None) -> None:
        self._set_status_card("channels", str(len(self._selected_channels())))

    def _browse_log_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择 EVE Chatlogs 目录",
            self.log_dir_edit.text(),
        )
        if selected:
            self.log_dir_edit.setText(selected)
            self._refresh_channels()

    def _open_log_dir(self) -> None:
        path = Path(self.log_dir_edit.text())
        if not path.is_dir():
            QMessageBox.warning(self, "目录不存在", "请先选择有效的 EVE Chatlogs 目录。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _start(self) -> None:
        config = self._current_config()
        error = server_url_validation_error(config.server_url)
        if not config.server_url:
            error = "请填写服务端地址。"
        elif not Path(config.log_dir).is_dir():
            error = "EVE Chatlogs 目录不存在。"
        elif not config.selected_channels:
            error = "请至少选择一个需要解析的预警频道。"
        api_key = self.key_edit.text().strip()
        if not error and not is_valid_api_key(api_key, allow_empty=True):
            error = "设备密钥格式无效，请重新复制完整密钥。"
        if error:
            QMessageBox.warning(self, "无法启动", error)
            return

        try:
            save_channel_client_config(config, self._config_path)
            self._auth_store.set_api_key(api_key)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "保存配置失败", str(exc))
            return
        self._config = config

        self._thread = QThread(self)
        self._worker = ChannelClientWorker(config, api_key)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._on_status)
        self._worker.line_processed.connect(self._on_line)
        self._worker.stopped.connect(self._thread.quit)
        self._worker.stopped.connect(self._on_stopped)
        self._thread.finished.connect(self._thread.deleteLater)
        self._set_controls_running(True)
        self._thread.start()

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self._set_status_card("state", "正在停止", "warn")
        self.stop_button.setEnabled(False)

    def _set_controls_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.server_edit.setEnabled(not running)
        self.key_edit.setEnabled(not running)
        self.log_dir_edit.setEnabled(not running)
        self.channel_list.setEnabled(not running)
        self.interval_spin.setEnabled(not running)
        self.ignore_existing_check.setEnabled(not running)
        self.start_button.setStyleSheet(monitor_button_style(active=running))

    def _on_status(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("state") or "idle")
        labels = {
            "running": ("监听中", "ok"),
            "warning": ("连接异常", "warn"),
            "error": ("运行失败", "danger"),
        }
        label, tone = labels.get(state, ("未启动", "idle"))
        self._set_status_card("state", label, tone)
        self._set_status_card("uploaded", str(payload.get("uploaded") or 0))
        last_success = str(payload.get("last_success_at") or "")
        self._set_status_card("success", self._display_time(last_success))
        error = str(payload.get("last_error") or "")
        self.error_label.setText(error)
        self.error_label.setStyleSheet("color: #f0b35a;" if state == "warning" else "color: #e66a76;")
        self.error_label.setVisible(bool(error))

    def _on_line(self, payload: dict[str, Any]) -> None:
        self.event_table.insertRow(0)
        for column, key in enumerate(("time", "channel", "result", "text")):
            self.event_table.setItem(0, column, QTableWidgetItem(str(payload.get(key) or "")))
        while self.event_table.rowCount() > MAX_EVENT_ROWS:
            self.event_table.removeRow(self.event_table.rowCount() - 1)

    def _on_stopped(self) -> None:
        self._worker = None
        self._thread = None
        self._set_controls_running(False)
        self._set_status_card("state", "已停止", "idle")

    def _set_status_card(self, key: str, value: str, tone: str = "idle") -> None:
        card, label = self._status_cards[key]
        label.setText(value)
        card.setStyleSheet(status_card_style(tone))

    @staticmethod
    def _display_time(value: str) -> str:
        if not value:
            return "—"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone().strftime("%H:%M:%S")
        except ValueError:
            return value[-8:]

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        event.accept()


def main() -> int:
    """Run the standalone channel log client."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("EVE Sentry Channel Client")
    window = ChannelClientWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
