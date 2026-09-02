"""Settings panel for OCR scan and window configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtCore import QRegularExpression, pyqtSignal
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.channels.log_watcher import DEFAULT_CHATLOG_DIR
from app.channels.identity_logs import ClientAuthStateStore
from app.intel_client import INVALID_API_KEY_MESSAGE
from app.version import current_version


DEFAULT_ALERT_VOLUME = 1.0
DEFAULT_ALERT_REPEAT_INTERVAL = 2
DEFAULT_ALERT_REPEAT_COUNT = 3
DEFAULT_ALERT_SOUND_MODE = "interval"
SETTINGS_INPUT_HEIGHT = 30
SETTINGS_LONG_INPUT_WIDTH = 198
SETTINGS_INLINE_INPUT_WIDTH = 136
SETTINGS_NUMBER_INPUT_WIDTH = 60
MAX_AUTH_STATUS_LENGTH = 160


def normalize_server_url(value: Any) -> str:
    """Return a normalized HTTP server URL for the desktop clients."""
    url = str(value or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def server_url_validation_error(value: Any) -> str:
    """Return a user-facing validation error for a configured server URL."""
    url = normalize_server_url(value)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return "服务端地址端口无效，请填写 1-65535 的数字端口"
    if parsed.scheme.casefold() not in {"http", "https"}:
        return "服务端地址仅支持 http:// 或 https://"
    if not parsed.hostname:
        return "服务端地址缺少有效的主机名或 IP 地址"
    if parsed.username or parsed.password:
        return "服务端地址不能包含用户名或密码"
    return ""


def default_channel_settings_path() -> Path:
    """Return the legacy local settings path used by the monitor client."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "EVE Sentry" / "channel_settings.json"
    return Path.home() / ".eve-sentry" / "channel_settings.json"


class SettingsPanel(QWidget):
    """Left-side control panel for OCR scan settings."""

    scan_settings_changed = pyqtSignal()
    server_url_changed = pyqtSignal(str)
    api_key_changed = pyqtSignal(str)
    update_requested = pyqtSignal()
    behavior_settings_changed = pyqtSignal()
    diagnostics_requested = pyqtSignal()

    def __init__(self, parent=None, config_path: str | Path | None = None):
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self._config_path = (
            Path(config_path) if config_path else default_channel_settings_path()
        )
        config = self._load_channel_config()
        self._chatlog_dir = str(config["chatlog_dir"])
        self._auth_state_store = ClientAuthStateStore()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 8)
        layout.setSpacing(5)

        brand = QLabel("EVE SENTRY")
        brand.setObjectName("brandTitle")
        layout.addWidget(brand)

        product = QLabel("监控客户端")
        product.setObjectName("brandMeta")
        layout.addWidget(product)

        server_group = QGroupBox()
        server_layout = QVBoxLayout(server_group)
        server_layout.setContentsMargins(0, 4, 0, 0)
        server_layout.setSpacing(4)
        server_label = QLabel("服务端")
        server_label.setObjectName("fieldLabel")
        server_layout.addWidget(server_label)
        self._server_url_edit = QLineEdit(str(config["server_url"]))
        self._server_url_edit.setFixedSize(
            SETTINGS_LONG_INPUT_WIDTH,
            SETTINGS_INPUT_HEIGHT,
        )
        self._server_url_edit.setPlaceholderText("请输入服务端地址")
        self._server_url_edit.setClearButtonEnabled(True)
        self._server_url_edit.setToolTip("支持 http:// 或 https://；留空可关闭服务端通信")
        server_layout.addWidget(self._server_url_edit)
        key_label = QLabel("设备密钥")
        key_label.setObjectName("fieldLabel")
        server_layout.addWidget(key_label)
        self._api_key_edit = QLineEdit(self._auth_state_store.api_key())
        self._api_key_edit.setFixedSize(
            SETTINGS_LONG_INPUT_WIDTH,
            SETTINGS_INPUT_HEIGHT,
        )
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("eve_...")
        self._api_key_edit.setClearButtonEnabled(True)
        self._api_key_edit.setToolTip("留空表示不启用认证；清空后会停止使用旧密钥")
        self._api_key_edit.setMaxLength(128)
        self._api_key_edit.setValidator(
            QRegularExpressionValidator(
                QRegularExpression(r"(?:eve_[A-Za-z0-9_-]*)?"),
                self._api_key_edit,
            )
        )
        self._api_key_edit.inputRejected.connect(
            lambda: self.set_auth_status(INVALID_API_KEY_MESSAGE, error=True)
        )
        server_layout.addWidget(self._api_key_edit)
        self._auth_status_label = QLabel("未启用认证")
        self._auth_status_label.setObjectName("authStatus")
        self._auth_status_label.setWordWrap(True)
        server_layout.addWidget(self._auth_status_label)
        layout.addWidget(server_group)

        scan_group = QGroupBox()
        scan_layout = QVBoxLayout(scan_group)
        scan_layout.setContentsMargins(0, 4, 0, 0)
        scan_layout.setSpacing(4)

        interval_row = QHBoxLayout()
        interval_label = QLabel("扫描间隔")
        interval_label.setObjectName("fieldLabel")
        interval_row.addWidget(interval_label)
        interval_row.addStretch()
        self._interval_spin = QSpinBox()
        self._interval_spin.setFixedSize(
            SETTINGS_NUMBER_INPUT_WIDTH,
            SETTINGS_INPUT_HEIGHT,
        )
        self._interval_spin.setRange(1, 10)
        self._interval_spin.setValue(int(config["scan_interval"]))
        interval_row.addWidget(self._interval_spin)
        interval_unit = QLabel("秒")
        interval_unit.setObjectName("inputUnit")
        interval_row.addWidget(interval_unit)
        scan_layout.addLayout(interval_row)

        self._ocr_enabled_check = QCheckBox("OCR 姓名识别")
        self._ocr_enabled_check.setChecked(bool(config["ocr_enabled"]))
        scan_layout.addWidget(self._ocr_enabled_check)

        keyword_row = QHBoxLayout()
        keyword_label = QLabel("窗口关键字")
        keyword_label.setObjectName("fieldLabel")
        keyword_row.addWidget(keyword_label)
        self._keyword_edit = QLineEdit(str(config["window_keyword"]))
        self._keyword_edit.setFixedSize(
            SETTINGS_INLINE_INPUT_WIDTH,
            SETTINGS_INPUT_HEIGHT,
        )
        keyword_row.addWidget(self._keyword_edit)
        scan_layout.addLayout(keyword_row)

        layout.addWidget(scan_group)

        self._behavior_group = QGroupBox("启动与托盘")
        self._behavior_group.setObjectName("behaviorPanel")
        behavior_layout = QHBoxLayout(self._behavior_group)
        behavior_layout.setContentsMargins(10, 8, 10, 8)
        behavior_layout.setSpacing(12)
        self._start_with_windows_check = QCheckBox("开机启动")
        self._start_with_windows_check.setChecked(bool(config["start_with_windows"]))
        self._start_minimized_check = QCheckBox("启动后最小化")
        self._start_minimized_check.setChecked(bool(config["start_minimized"]))
        self._close_to_tray_check = QCheckBox("关闭到托盘")
        self._close_to_tray_check.setChecked(bool(config["close_to_tray"]))
        self._restore_monitor_check = QCheckBox("恢复上次监控状态")
        self._restore_monitor_check.setChecked(bool(config["restore_monitor_state"]))
        for checkbox in (
            self._start_with_windows_check,
            self._start_minimized_check,
            self._close_to_tray_check,
            self._restore_monitor_check,
        ):
            behavior_layout.addWidget(checkbox)
            checkbox.toggled.connect(self._on_behavior_settings_changed)
        self._behavior_group.hide()

        alert_group = QGroupBox()
        alert_layout = QVBoxLayout(alert_group)
        alert_layout.setContentsMargins(0, 4, 0, 0)
        alert_layout.setSpacing(4)
        self._alert_sound_check = QCheckBox("告警声音")
        self._alert_sound_check.setChecked(bool(config["alert_sound_enabled"]))
        alert_layout.addWidget(self._alert_sound_check)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("播放方式"))
        mode_row.addStretch()
        self._alert_sound_mode_combo = QComboBox()
        self._alert_sound_mode_combo.addItem("按间隔响", "interval")
        self._alert_sound_mode_combo.addItem("持续响", "continuous")
        mode_index = self._alert_sound_mode_combo.findData(config["alert_sound_mode"])
        self._alert_sound_mode_combo.setCurrentIndex(max(0, mode_index))
        self._alert_sound_mode_combo.setFixedHeight(SETTINGS_INPUT_HEIGHT)
        self._alert_sound_mode_combo.setToolTip("持续响会在仍有敌对目标时循环播放")
        mode_row.addWidget(self._alert_sound_mode_combo)
        alert_layout.addLayout(mode_row)
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("播放间隔"))
        interval_row.addStretch()
        self._alert_repeat_interval_spin = QSpinBox()
        self._alert_repeat_interval_spin.setRange(1, 60)
        self._alert_repeat_interval_spin.setFixedSize(
            SETTINGS_NUMBER_INPUT_WIDTH,
            SETTINGS_INPUT_HEIGHT,
        )
        self._alert_repeat_interval_spin.setValue(config["alert_repeat_interval"])
        interval_row.addWidget(self._alert_repeat_interval_spin)
        repeat_interval_unit = QLabel("秒")
        repeat_interval_unit.setObjectName("inputUnit")
        interval_row.addWidget(repeat_interval_unit)
        alert_layout.addLayout(interval_row)
        count_row = QHBoxLayout()
        count_row.addWidget(QLabel("播放次数"))
        count_row.addStretch()
        self._alert_repeat_count_spin = QSpinBox()
        self._alert_repeat_count_spin.setRange(1, 10)
        self._alert_repeat_count_spin.setFixedSize(
            SETTINGS_NUMBER_INPUT_WIDTH,
            SETTINGS_INPUT_HEIGHT,
        )
        self._alert_repeat_count_spin.setValue(config["alert_repeat_count"])
        count_row.addWidget(self._alert_repeat_count_spin)
        repeat_count_unit = QLabel("次")
        repeat_count_unit.setObjectName("inputUnit")
        count_row.addWidget(repeat_count_unit)
        alert_layout.addLayout(count_row)
        for signal in (
            self._alert_sound_check.toggled,
            self._alert_sound_mode_combo.currentIndexChanged,
            self._alert_repeat_interval_spin.valueChanged,
            self._alert_repeat_count_spin.valueChanged,
        ):
            signal.connect(self._on_behavior_settings_changed)
        layout.addWidget(alert_group)

        version_group = QGroupBox()
        version_layout = QVBoxLayout(version_group)
        version_layout.setContentsMargins(0, 4, 0, 0)
        version_layout.setSpacing(4)
        self._update_status_label = QLabel(f"当前 v{current_version()}")
        self._update_status_label.setObjectName("authStatus")
        self._update_status_label.setWordWrap(True)
        version_layout.addWidget(self._update_status_label)
        layout.addStretch()
        layout.addWidget(version_group)

        self._update_button = QPushButton("检查更新")
        self._update_button.setObjectName("secondaryAction")
        self._update_button.clicked.connect(self.update_requested.emit)
        layout.addWidget(self._update_button)

        diagnostics_button = QPushButton("导出诊断包")
        diagnostics_button.setObjectName("secondaryAction")
        diagnostics_button.clicked.connect(self.diagnostics_requested.emit)
        layout.addWidget(diagnostics_button)

        self._interval_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._ocr_enabled_check.toggled.connect(self._on_scan_settings_changed)
        self._keyword_edit.editingFinished.connect(self._on_scan_settings_changed)
        self._server_url_edit.editingFinished.connect(self._on_server_url_changed)
        self._api_key_edit.editingFinished.connect(self._on_api_key_changed)

    def get_interval(self) -> float:
        return float(self._interval_spin.value())

    def get_keyword(self) -> str:
        return self._keyword_edit.text().strip()

    def get_ocr_enabled(self) -> bool:
        return self._ocr_enabled_check.isChecked()

    def get_server_url(self) -> str:
        return normalize_server_url(self._server_url_edit.text())

    def get_channel_log_dir(self) -> str:
        """Return the cached EVE Chatlogs directory without rescanning it."""
        configured = os.environ.get("EVE_SENTRY_CHATLOG_DIR", "").strip()
        if configured:
            resolved = os.path.expandvars(configured)
        else:
            resolved = self._chatlog_dir or str(DEFAULT_CHATLOG_DIR)
        self._chatlog_dir = str(resolved)
        return self._chatlog_dir

    def get_api_key(self) -> str:
        return self._api_key_edit.text().strip()

    def get_start_with_windows(self) -> bool:
        return self._start_with_windows_check.isChecked()

    def get_start_minimized(self) -> bool:
        return self._start_minimized_check.isChecked()

    def get_close_to_tray(self) -> bool:
        return self._close_to_tray_check.isChecked()

    def get_restore_monitor_state(self) -> bool:
        return self._restore_monitor_check.isChecked()

    def set_behavior_preference(self, name: str, enabled: bool) -> None:
        """Update a startup preference selected from the tray menu."""
        checkboxes = {
            "start_with_windows": self._start_with_windows_check,
            "start_minimized": self._start_minimized_check,
            "close_to_tray": self._close_to_tray_check,
            "restore_monitor_state": self._restore_monitor_check,
        }
        checkbox = checkboxes.get(str(name))
        if checkbox is None:
            raise KeyError(f"Unknown behavior preference: {name}")
        checkbox.setChecked(bool(enabled))

    def get_alert_preferences(self) -> dict[str, Any]:
        return {
            "muted": not self._alert_sound_check.isChecked(),
            "volume": DEFAULT_ALERT_VOLUME,
            "sound_mode": self._alert_sound_mode_combo.currentData() or DEFAULT_ALERT_SOUND_MODE,
            "repeat_interval": float(self._alert_repeat_interval_spin.value()),
            "repeat_count": self._alert_repeat_count_spin.value(),
        }

    def auth_state_store(self) -> ClientAuthStateStore:
        return self._auth_state_store

    def set_auth_status(self, message: str, error: bool = False) -> None:
        color = "#ff6b73" if error else "#37d6b0"
        text = " ".join(str(message or "").split())
        lowered = text.casefold()
        if "illegal header value" in lowered and "bearer" in lowered:
            text = INVALID_API_KEY_MESSAGE
        elif len(text) > MAX_AUTH_STATUS_LENGTH:
            text = f"{text[:MAX_AUTH_STATUS_LENGTH - 3]}..."
        self._auth_status_label.setStyleSheet(f"color: {color};")
        self._auth_status_label.setText(text)

    def set_update_state(
        self,
        message: str,
        action_text: str,
        enabled: bool,
    ) -> None:
        """Update the compact release status and action button."""
        self._update_status_label.setText(str(message or ""))
        self._update_button.setText(str(action_text or "检查更新"))
        self._update_button.setEnabled(bool(enabled))

    def save_channel_config(self) -> None:
        """Persist local monitor settings using the existing settings file."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(self._channel_config_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _on_scan_settings_changed(self, _value=None) -> None:
        self.save_channel_config()
        self.scan_settings_changed.emit()

    def _on_server_url_changed(self) -> None:
        server_url = self.get_server_url()
        validation_error = server_url_validation_error(server_url)
        if validation_error:
            self._server_url_edit.setProperty("validationState", "error")
            self._server_url_edit.setToolTip(validation_error)
            self._server_url_edit.style().unpolish(self._server_url_edit)
            self._server_url_edit.style().polish(self._server_url_edit)
            self._server_url_edit.update()
            self.set_auth_status(validation_error, error=True)
            return
        self._server_url_edit.setProperty("validationState", "ok")
        self._server_url_edit.setToolTip(
            "支持 http:// 或 https://；留空可关闭服务端通信"
        )
        self._server_url_edit.style().unpolish(self._server_url_edit)
        self._server_url_edit.style().polish(self._server_url_edit)
        self._server_url_edit.update()
        self._server_url_edit.setText(server_url)
        self.save_channel_config()
        self.server_url_changed.emit(server_url)

    def _on_api_key_changed(self) -> None:
        api_key = self.get_api_key()
        if api_key and not self._api_key_edit.hasAcceptableInput():
            self.set_auth_status(INVALID_API_KEY_MESSAGE, error=True)
            return
        try:
            changed = self._auth_state_store.set_api_key(api_key)
        except ValueError:
            self.set_auth_status(INVALID_API_KEY_MESSAGE, error=True)
            return
        self.set_auth_status("等待身份校验" if api_key else "未启用认证")
        if changed:
            self.api_key_changed.emit(api_key)

    def _on_behavior_settings_changed(self, _value=None) -> None:
        self.save_channel_config()
        self.behavior_settings_changed.emit()

    def _load_channel_config(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except (OSError, json.JSONDecodeError):
            payload = {}

        env_chatlog_dir = os.environ.get("EVE_SENTRY_CHATLOG_DIR", "").strip()
        if env_chatlog_dir:
            chatlog_dir = os.path.expandvars(env_chatlog_dir)
        else:
            chatlog_dir = str(
                payload.get("chatlog_dir") or DEFAULT_CHATLOG_DIR
            )
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
        # An explicitly configured environment value may override the file,
        # but an empty variable must not erase a previously saved address.
        env_server_url = os.environ.get("EVE_SENTRY_INTEL_URL", "").strip()
        server_url = normalize_server_url(
            env_server_url or payload.get("server_url", "")
        )
        return {
            "chatlog_dir": chatlog_dir or str(DEFAULT_CHATLOG_DIR),
            "scan_interval": scan_interval,
            "ocr_enabled": bool(payload.get("ocr_enabled", True)),
            "window_keyword": window_keyword,
            "server_url": server_url,
            "start_with_windows": bool(payload.get("start_with_windows", False)),
            "start_minimized": bool(payload.get("start_minimized", False)),
            "close_to_tray": bool(payload.get("close_to_tray", True)),
            "restore_monitor_state": bool(
                payload.get("restore_monitor_state", True)
            ),
            "alert_sound_enabled": bool(
                payload.get(
                    "alert_sound_enabled",
                    not bool(payload.get("alert_muted", False)),
                )
            ),
            "alert_sound_mode": self._clean_alert_sound_mode(
                payload.get("alert_sound_mode", DEFAULT_ALERT_SOUND_MODE)
            ),
            "alert_repeat_interval": self._clean_alert_repeat_interval(
                payload.get("alert_repeat_interval", DEFAULT_ALERT_REPEAT_INTERVAL)
            ),
            "alert_repeat_count": self._clean_alert_repeat_count(
                payload.get("alert_repeat_count", DEFAULT_ALERT_REPEAT_COUNT)
            ),
        }

    def _channel_config_payload(self) -> dict[str, Any]:
        return {
            "chatlog_dir": self.get_channel_log_dir(),
            "scan_interval": int(self._interval_spin.value()),
            "ocr_enabled": self.get_ocr_enabled(),
            "window_keyword": self.get_keyword(),
            "server_url": self.get_server_url(),
            "start_with_windows": self.get_start_with_windows(),
            "start_minimized": self.get_start_minimized(),
            "close_to_tray": self.get_close_to_tray(),
            "restore_monitor_state": self.get_restore_monitor_state(),
            "alert_sound_enabled": self._alert_sound_check.isChecked(),
            "alert_sound_mode": self._alert_sound_mode_combo.currentData() or DEFAULT_ALERT_SOUND_MODE,
            "alert_repeat_interval": self._alert_repeat_interval_spin.value(),
            "alert_repeat_count": self._alert_repeat_count_spin.value(),
        }

    def _clean_scan_interval(self, value: Any) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 2

    def _clean_alert_repeat_interval(self, value: Any) -> int:
        try:
            return max(1, min(60, int(value)))
        except (TypeError, ValueError):
            return DEFAULT_ALERT_REPEAT_INTERVAL

    def _clean_alert_sound_mode(self, value: Any) -> str:
        mode = str(value or "").strip().casefold()
        return mode if mode in {"interval", "continuous"} else DEFAULT_ALERT_SOUND_MODE

    def _clean_alert_repeat_count(self, value: Any) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return DEFAULT_ALERT_REPEAT_COUNT
