"""Settings panel: scan, window, and channel configuration."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.channels.log_watcher import DEFAULT_CHATLOG_DIR, channel_name_from_path


class ChannelListWidget(QListWidget):
    """Channel list whose entire row toggles the checkbox exactly once."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed_item: QListWidgetItem | None = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_item = self.itemAt(event.position().toPoint())
            if self._pressed_item is not None:
                self.setCurrentItem(self._pressed_item)
                self.setFocus()
                event.accept()
                return
        self._pressed_item = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            pressed_item = self._pressed_item
            self._pressed_item = None
            if item is not None and item is pressed_item:
                next_state = (
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(next_state)
                event.accept()
                return
        super().mouseReleaseEvent(event)


def default_channel_settings_path() -> Path:
    """Return the local client channel settings path."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "EVE Sentry" / "channel_settings.json"
    return Path.home() / ".eve-sentry" / "channel_settings.json"


class SettingsPanel(QWidget):
    """Left-side control panel for monitor and channel settings."""

    scan_settings_changed = pyqtSignal()
    channel_settings_changed = pyqtSignal()

    def __init__(self, parent=None, config_path: str | Path | None = None):
        super().__init__(parent)
        self._config_path = Path(config_path) if config_path else default_channel_settings_path()
        self._discovered_channel_names: list[str] = []
        channel_config = self._load_channel_config()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        scan_group = QGroupBox("扫描设置")
        scan_layout = QVBoxLayout(scan_group)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("扫描间隔"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 10)
        self._interval_spin.setValue(int(channel_config["scan_interval"]))
        self._interval_spin.setSuffix(" 秒")
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        scan_layout.addLayout(interval_row)

        keyword_row = QHBoxLayout()
        keyword_row.addWidget(QLabel("窗口关键字"))
        self._keyword_edit = QLineEdit(str(channel_config["window_keyword"]))
        keyword_row.addWidget(self._keyword_edit)
        scan_layout.addLayout(keyword_row)

        scan_hint = QLabel("运行中修改扫描间隔会立即生效")
        scan_hint.setWordWrap(True)
        scan_layout.addWidget(scan_hint)

        layout.addWidget(scan_group)

        channel_group = QGroupBox("预警设置")
        channel_layout = QVBoxLayout(channel_group)

        self._channel_enabled = QCheckBox("开启预警频道监控")
        self._channel_enabled.setChecked(bool(channel_config["enabled"]))
        channel_layout.addWidget(self._channel_enabled)

        channel_layout.addWidget(QLabel("预警频道名 / 通配符"))
        self._channel_edit = QLineEdit(str(channel_config["channels"]))
        self._channel_edit.setPlaceholderText(
            "完整频道名或通配符，例如: wc.Venal+Br+Te, *Intel"
        )
        channel_layout.addWidget(self._channel_edit)

        channel_layout.addWidget(QLabel("EVE Chatlogs 目录"))
        dir_row = QHBoxLayout()
        self._channel_log_dir_edit = QLineEdit(str(channel_config["chatlog_dir"]))
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(self._browse_channel_log_dir)
        dir_row.addWidget(self._channel_log_dir_edit)
        dir_row.addWidget(browse_btn)
        channel_layout.addLayout(dir_row)

        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("历史频道过滤"))
        self._channel_recent_days_spin = QSpinBox()
        self._channel_recent_days_spin.setRange(0, 365)
        self._channel_recent_days_spin.setSpecialValueText("不过滤")
        self._channel_recent_days_spin.setSuffix(" 天")
        self._channel_recent_days_spin.setValue(int(channel_config["recent_days"]))
        self._channel_recent_days_spin.valueChanged.connect(
            lambda _value: self._refresh_channel_list(show_message=False)
        )
        history_row.addWidget(self._channel_recent_days_spin)
        history_row.addStretch()
        channel_layout.addLayout(history_row)

        discover_row = QHBoxLayout()
        discover_btn = QPushButton("识别频道")
        discover_btn.clicked.connect(lambda: self._refresh_channel_list(show_message=True))
        discover_row.addWidget(QLabel("已识别频道"))
        discover_row.addStretch()
        discover_row.addWidget(discover_btn)
        channel_layout.addLayout(discover_row)

        self._channel_list = ChannelListWidget()
        self._channel_list.setMaximumHeight(120)
        self._channel_list.itemChanged.connect(self._on_channel_item_changed)
        channel_layout.addWidget(self._channel_list)

        save_btn = QPushButton("应用预警配置")
        save_btn.clicked.connect(self._apply_channel_config)
        channel_layout.addWidget(save_btn)

        layout.addWidget(channel_group)
        layout.addStretch()
        self._refresh_channel_list(show_message=False)
        self._interval_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._keyword_edit.editingFinished.connect(self._on_scan_settings_changed)
        self._channel_enabled.toggled.connect(self._on_channel_settings_changed)
        self._channel_edit.editingFinished.connect(self._on_channel_settings_changed)
        self._channel_log_dir_edit.editingFinished.connect(
            self._on_channel_settings_changed
        )
        self._channel_recent_days_spin.valueChanged.connect(
            self._on_channel_settings_changed
        )

    def get_interval(self) -> float:
        return float(self._interval_spin.value())

    def get_keyword(self) -> str:
        return self._keyword_edit.text().strip()

    def get_channel_names(self) -> list[str]:
        """Return selected channel filters; empty means no channel submission."""
        if not self._channel_enabled.isChecked():
            return []
        return self._configured_channel_names()

    def get_channel_log_dir(self) -> str:
        return self._channel_log_dir_edit.text().strip() or str(DEFAULT_CHATLOG_DIR)

    def save_channel_config(self, show_message: bool = False) -> None:
        """Persist scan and alert-channel settings locally."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(self._channel_config_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if show_message:
            QMessageBox.information(self, "预警配置", "预警与扫描配置已应用")

    def _on_scan_settings_changed(self, _value=None) -> None:
        self.save_channel_config()
        self.scan_settings_changed.emit()

    def _on_channel_settings_changed(self, _value=None) -> None:
        self.save_channel_config()
        self.channel_settings_changed.emit()

    def _apply_channel_config(self) -> None:
        self.save_channel_config(show_message=True)
        self.channel_settings_changed.emit()

    def _browse_channel_log_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择 EVE Chatlogs 目录",
            self.get_channel_log_dir(),
        )
        if selected:
            self._channel_log_dir_edit.setText(selected)
            self._refresh_channel_list(show_message=False)
            self._on_channel_settings_changed()

    def _refresh_channel_list(self, show_message: bool = False) -> None:
        selected = {name.casefold() for name in self._configured_channel_names()}
        channels = self._discover_channel_names()
        self._discovered_channel_names = channels
        self._channel_list.blockSignals(True)
        self._channel_list.clear()
        for name in channels:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if name.casefold() in selected
                else Qt.CheckState.Unchecked
            )
            self._channel_list.addItem(item)
        self._channel_list.blockSignals(False)
        if show_message:
            QMessageBox.information(
                self,
                "频道识别",
                f"已识别 {len(channels)} 个频道" if channels else "未识别到频道日志文件",
            )

    def _on_channel_item_changed(self, _item: QListWidgetItem) -> None:
        discovered = {name.casefold() for name in self._discovered_channel_names}
        manual_patterns = [
            name
            for name in self._parse_channel_text(self._channel_edit.text())
            if any(char in name for char in "*?") or name.casefold() not in discovered
        ]
        names = self._checked_channel_names() + manual_patterns
        self._channel_edit.setText(", ".join(self._dedupe_channel_names(names)))
        self._on_channel_settings_changed()

    def _checked_channel_names(self) -> list[str]:
        names: list[str] = []
        for index in range(self._channel_list.count()):
            item = self._channel_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                names.append(item.text())
        return names

    def _configured_channel_names(self) -> list[str]:
        return self._dedupe_channel_names(
            self._checked_channel_names()
            + self._parse_channel_text(self._channel_edit.text())
        )

    def _discover_channel_names(self) -> list[str]:
        log_dir = Path(self.get_channel_log_dir())
        if not log_dir.exists():
            return []
        channels: dict[str, str] = {}
        for path in log_dir.glob("*.txt"):
            if not path.is_file():
                continue
            if self._channel_file_is_historical(path):
                continue
            name = channel_name_from_path(path)
            key = name.casefold()
            if name and key not in channels:
                channels[key] = name
        return sorted(channels.values(), key=str.casefold)

    def _channel_file_is_historical(self, path: Path) -> bool:
        recent_days = int(self._channel_recent_days_spin.value())
        if recent_days <= 0:
            return False
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return True
        cutoff = time.time() - (recent_days * 24 * 60 * 60)
        return modified_at < cutoff

    def _load_channel_config(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except (OSError, json.JSONDecodeError):
            payload = {}

        env_channel = os.environ.get("EVE_SENTRY_CHANNEL")
        env_chatlog_dir = os.environ.get("EVE_SENTRY_CHATLOG_DIR")
        channels = str(
            env_channel
            if env_channel is not None
            else payload.get("channels", "")
        ).strip()
        chatlog_dir = str(
            env_chatlog_dir
            if env_chatlog_dir is not None
            else payload.get("chatlog_dir", str(DEFAULT_CHATLOG_DIR))
        ).strip()
        enabled = payload.get("enabled")
        recent_days = self._clean_recent_days(payload.get("recent_days", 30))
        scan_interval = self._clean_scan_interval(
            os.environ.get(
                "EVE_SENTRY_SCAN_INTERVAL",
                payload.get("scan_interval", 2),
            )
        )
        window_keyword = str(
            os.environ.get(
                "EVE_SENTRY_WINDOW_KEYWORD",
                payload.get("window_keyword", "EVE -"),
            )
        ).strip() or "EVE -"
        if env_channel is not None:
            enabled = bool(channels)
        elif enabled is None:
            enabled = bool(channels)
        return {
            "enabled": bool(enabled),
            "channels": channels,
            "chatlog_dir": chatlog_dir or str(DEFAULT_CHATLOG_DIR),
            "recent_days": recent_days,
            "scan_interval": scan_interval,
            "window_keyword": window_keyword,
        }

    def _channel_config_payload(self) -> dict[str, Any]:
        return {
            "enabled": self._channel_enabled.isChecked(),
            "channels": ", ".join(self._configured_channel_names()),
            "chatlog_dir": self.get_channel_log_dir(),
            "recent_days": int(self._channel_recent_days_spin.value()),
            "scan_interval": int(self._interval_spin.value()),
            "window_keyword": self.get_keyword(),
        }

    def _clean_scan_interval(self, value: Any) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 2

    def _clean_recent_days(self, value: Any) -> int:
        try:
            return max(0, min(365, int(value)))
        except (TypeError, ValueError):
            return 30

    def _parse_channel_text(self, text: str) -> list[str]:
        return self._dedupe_channel_names(
            item.strip()
            for item in str(text or "").replace(";", ",").split(",")
            if item.strip()
        )

    def _dedupe_channel_names(self, names) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for name in names:
            key = str(name).casefold()
            if key and key not in seen:
                seen.add(key)
                result.append(str(name))
        return result
