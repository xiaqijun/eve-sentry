"""Settings panel for OCR scan and window configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.channels.log_watcher import DEFAULT_CHATLOG_DIR, resolve_chatlog_dir


DEFAULT_INTEL_URL = "http://114.132.167.239:8765"


def normalize_server_url(value: Any) -> str:
    """Return a normalized HTTP server URL for the desktop clients."""
    url = str(value or "").strip() or DEFAULT_INTEL_URL
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


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

    def __init__(self, parent=None, config_path: str | Path | None = None):
        super().__init__(parent)
        self._config_path = (
            Path(config_path) if config_path else default_channel_settings_path()
        )
        config = self._load_channel_config()
        self._chatlog_dir = str(config["chatlog_dir"])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        server_group = QGroupBox("服务端设置")
        server_layout = QVBoxLayout(server_group)
        server_layout.addWidget(QLabel("服务端地址"))
        self._server_url_edit = QLineEdit(str(config["server_url"]))
        self._server_url_edit.setPlaceholderText(DEFAULT_INTEL_URL)
        server_layout.addWidget(self._server_url_edit)
        layout.addWidget(server_group)

        scan_group = QGroupBox("扫描设置")
        scan_layout = QVBoxLayout(scan_group)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("扫描间隔"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 10)
        self._interval_spin.setValue(int(config["scan_interval"]))
        self._interval_spin.setSuffix(" 秒")
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        scan_layout.addLayout(interval_row)

        keyword_row = QHBoxLayout()
        keyword_row.addWidget(QLabel("窗口关键字"))
        self._keyword_edit = QLineEdit(str(config["window_keyword"]))
        keyword_row.addWidget(self._keyword_edit)
        scan_layout.addLayout(keyword_row)

        scan_hint = QLabel("运行中修改扫描间隔会立即生效")
        scan_hint.setWordWrap(True)
        scan_layout.addWidget(scan_hint)

        layout.addWidget(scan_group)
        layout.addStretch()

        self._interval_spin.valueChanged.connect(self._on_scan_settings_changed)
        self._keyword_edit.editingFinished.connect(self._on_scan_settings_changed)
        self._server_url_edit.editingFinished.connect(self._on_server_url_changed)

    def get_interval(self) -> float:
        return float(self._interval_spin.value())

    def get_keyword(self) -> str:
        return self._keyword_edit.text().strip()

    def get_server_url(self) -> str:
        return normalize_server_url(self._server_url_edit.text())

    def get_channel_log_dir(self) -> str:
        """Return the chatlog directory used only for local-system detection."""
        return self._chatlog_dir or str(DEFAULT_CHATLOG_DIR)

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
        self._server_url_edit.setText(server_url)
        self.save_channel_config()
        self.server_url_changed.emit(server_url)

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
            chatlog_dir = env_chatlog_dir
        else:
            chatlog_dir = str(
                resolve_chatlog_dir(
                    payload.get("chatlog_dir", DEFAULT_CHATLOG_DIR)
                )
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
        server_url = normalize_server_url(
            os.environ.get(
                "EVE_SENTRY_INTEL_URL",
                payload.get("server_url", DEFAULT_INTEL_URL),
            )
        )
        return {
            "chatlog_dir": chatlog_dir or str(DEFAULT_CHATLOG_DIR),
            "scan_interval": scan_interval,
            "window_keyword": window_keyword,
            "server_url": server_url,
        }

    def _channel_config_payload(self) -> dict[str, Any]:
        return {
            "chatlog_dir": self.get_channel_log_dir(),
            "scan_interval": int(self._interval_spin.value()),
            "window_keyword": self.get_keyword(),
            "server_url": self.get_server_url(),
        }

    def _clean_scan_interval(self, value: Any) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 2
