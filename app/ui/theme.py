"""Shared Qt theme helpers for the desktop client."""

from pathlib import Path
import sys


def _resource_path(name: str) -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return (root / "resources" / name).as_posix()


_SPIN_UP_ICON = _resource_path("spin-up.svg")
_SPIN_DOWN_ICON = _resource_path("spin-down.svg")

APP_QSS = """
QMainWindow {
    background: #090c10;
}
QWidget {
    background: transparent;
    color: #d9dde4;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 12px;
}
QWidget#appRoot {
    background: #090c10;
}
QWidget#workspace {
    background: #0b0f14;
}
QWidget#settingsPanel {
    background: #0e1218;
    border-right: 1px solid #252b34;
}
QWidget#settingsPanel QLineEdit,
QWidget#settingsPanel QComboBox,
QWidget#settingsPanel QSpinBox {
    min-height: 0;
    padding: 2px 8px;
}
QWidget#settingsPanel QSpinBox {
    padding: 1px 2px 1px 7px;
}
QWidget#settingsPanel QSpinBox::up-button,
QWidget#settingsPanel QSpinBox::down-button {
    width: 20px;
}
QWidget#settingsPanel QSpinBox::up-arrow,
QWidget#settingsPanel QSpinBox::down-arrow {
    width: 8px;
    height: 5px;
}
QWidget#settingsPanel QGroupBox {
    margin-top: 14px;
}
QWidget#settingsPanel QCheckBox {
    min-height: 24px;
    padding: 0 3px;
    spacing: 7px;
}
QWidget#settingsPanel QPushButton {
    min-height: 28px;
    padding: 4px 10px;
}
QLabel#brandTitle {
    color: #f4f7fa;
    font-size: 17px;
    font-weight: 700;
}
QLabel#brandMeta {
    color: #747d89;
    font-size: 11px;
}
QLabel#pageTitle {
    color: #f5f7fa;
    font-size: 19px;
    font-weight: 700;
}
QLabel#sectionTitle {
    color: #c7ccd4;
    font-size: 12px;
    font-weight: 600;
}
QLabel#fieldTitle {
    color: #8e97a4;
    font-weight: 600;
}
QLabel#fieldLabel {
    color: #858e9b;
    font-size: 11px;
}
QLabel#inputUnit {
    color: #737d89;
    font-size: 11px;
}
QLabel#targetMeta {
    color: #707987;
    font-size: 11px;
}
QLabel#statusCardTitle {
    color: #7e8793;
    font-size: 10px;
    font-weight: 600;
}
QLabel#statusCardValue {
    color: #f1f4f7;
    font-size: 13px;
    font-weight: 700;
}
QFrame#targetBar {
    border: 1px solid #252c35;
    border-radius: 5px;
    background: #11161d;
}
QGroupBox {
    border: none;
    border-top: 1px solid #252b34;
    border-radius: 0;
    margin-top: 20px;
    padding: 0;
    background: transparent;
    color: #c7ccd4;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 0;
    top: 2px;
    padding: 0 8px 0 0;
    background: #0e1218;
    color: #a8afb9;
}
QLabel {
    color: #b7bdc6;
}
QLineEdit, QComboBox, QSpinBox {
    min-height: 28px;
    border: 1px solid #303844;
    border-radius: 4px;
    padding: 3px 9px;
    background: #151a22;
    color: #edf0f4;
    selection-background-color: #0d7188;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #23b7d8;
}
QSpinBox {
    min-height: 32px;
    padding: 3px 30px 3px 8px;
}
QSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 26px;
    border: none;
    border-left: 1px solid #303844;
    border-bottom: 1px solid #303844;
    border-top-right-radius: 4px;
    background: #1b212a;
}
QSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 26px;
    border: none;
    border-left: 1px solid #303844;
    border-bottom-right-radius: 4px;
    background: #1b212a;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background: #242c36;
}
QSpinBox::up-arrow {
    image: url("__SPIN_UP_ICON__");
    width: 12px;
    height: 8px;
}
QSpinBox::down-arrow {
    image: url("__SPIN_DOWN_ICON__");
    width: 12px;
    height: 8px;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QListWidget, QTableWidget, QTextEdit {
    border: 1px solid #252c35;
    border-radius: 5px;
    background: #0d1117;
    color: #d8dde4;
    selection-background-color: #164d59;
    alternate-background-color: #11161d;
}
QListWidget::item {
    min-height: 28px;
    padding: 3px 6px;
}
QListWidget::indicator, QCheckBox::indicator {
    width: 18px;
    height: 18px;
}
QCheckBox {
    min-height: 28px;
    spacing: 8px;
    padding: 2px 4px;
}
QHeaderView::section {
    border: none;
    border-bottom: 1px solid #2b323c;
    padding: 6px 8px;
    background: #151a21;
    color: #89929e;
    font-weight: 600;
}
QTableWidget::item {
    padding: 4px 7px;
}
QPushButton {
    min-height: 32px;
    border: 1px solid #343d48;
    border-radius: 4px;
    padding: 5px 11px;
    background: #181e26;
    color: #e7eaee;
    font-weight: 600;
}
QPushButton:hover {
    border-color: #4c5967;
    background: #202731;
}
QPushButton:pressed {
    background: #272f3a;
}
QToolButton#iconButton {
    border: 1px solid #303844;
    border-radius: 4px;
    background: #181e26;
}
QToolButton#iconButton:hover {
    border-color: #4c5967;
    background: #222a34;
}
QToolButton#monitorWindowButton {
    border: 1px solid #303844;
    border-radius: 4px;
    padding: 5px 24px 5px 10px;
    background: #181e26;
    color: #e7eaee;
    font-weight: 600;
}
QToolButton#monitorWindowButton:hover,
QToolButton#monitorWindowButton:checked {
    border-color: #23b7d8;
    background: #202731;
}
QToolButton#monitorWindowButton[selectionState="empty"] {
    border-color: #d95752;
    background: #2b181b;
    color: #ff9a9f;
}
QToolButton#monitorWindowButton[selectionState="offline"] {
    border-color: #d89a3c;
    background: #2a2115;
    color: #f6c760;
}
QToolButton#monitorWindowButton::menu-indicator {
    subcontrol-position: right center;
    subcontrol-origin: padding;
    right: 8px;
}
QMenu {
    border: 1px solid #303844;
    border-radius: 4px;
    padding: 5px;
    background: #11161d;
    color: #e7eaee;
}
QMenu::item {
    min-width: 180px;
    padding: 7px 24px 7px 9px;
}
QMenu::item:selected {
    border-radius: 3px;
    background: #164d59;
}
QMenu::item:disabled {
    background: transparent;
    color: #707987;
}
QMenu::separator {
    height: 1px;
    margin: 5px 7px;
    background: #303844;
}
QTextEdit#runtimeLog {
    padding: 8px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}
QStatusBar {
    background: #040c12;
    border-top: 1px solid #252b34;
    color: #7f8895;
}
QToolTip {
    border: 1px solid #3a434f;
    background: #171c23;
    color: #edf0f4;
    padding: 4px 6px;
}
"""
APP_QSS = APP_QSS.replace("__SPIN_UP_ICON__", _SPIN_UP_ICON).replace(
    "__SPIN_DOWN_ICON__", _SPIN_DOWN_ICON
)

STATUS_CARD_BASE_QSS = """
QFrame {
    border: 1px solid %(border)s;
    border-left: 3px solid %(border)s;
    border-radius: 5px;
    background: %(background)s;
}
QLabel {
    border: none;
    background: transparent;
}
"""

STATUS_TONES = {
    "idle": ("#3a434f", "#12171e"),
    "ok": ("#2ca889", "#12171e"),
    "warn": ("#c38a32", "#17171a"),
    "danger": ("#d3504a", "#191619"),
    "active": ("#2e9fbd", "#12171e"),
}


def monitor_button_style(active: bool) -> str:
    """Return the prominent start/stop button style."""
    if active:
        return (
            "QPushButton { background: #8f2f2d; color: #fff5f3; "
            "border: 1px solid #d95752; border-radius: 4px; "
            "padding: 5px 14px; font-size: 13px; font-weight: 700; }"
            "QPushButton:hover { background: #a83a36; border-color: #ee6a65; }"
        )
    return (
        "QPushButton { background: #0d5f75; color: #f0fbfd; "
        "border: 1px solid #23b7d8; border-radius: 4px; "
        "padding: 5px 14px; font-size: 13px; font-weight: 700; }"
        "QPushButton:hover { background: #11718a; border-color: #51c8e2; }"
    )


def status_card_style(tone: str) -> str:
    """Return a status card stylesheet for a named tone."""
    border, background = STATUS_TONES.get(tone, STATUS_TONES["idle"])
    return STATUS_CARD_BASE_QSS % {"border": border, "background": background}
