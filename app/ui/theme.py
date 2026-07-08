"""Shared Qt theme helpers for the desktop client."""

APP_QSS = """
QMainWindow, QWidget {
    background: #061017;
    color: #d8e8ef;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 12px;
}
QGroupBox {
    border: 1px solid #163848;
    border-radius: 6px;
    margin-top: 12px;
    padding: 10px 8px 8px 8px;
    background: #081720;
    color: #e9f6fb;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #8bdaf1;
}
QLabel {
    color: #bdd0d8;
}
QLineEdit, QComboBox, QSpinBox {
    min-height: 26px;
    border: 1px solid #1c4254;
    border-radius: 4px;
    padding: 3px 8px;
    background: #071b26;
    color: #edf8fc;
    selection-background-color: #0d88a8;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QListWidget, QTableWidget, QTextEdit {
    border: 1px solid #153849;
    border-radius: 6px;
    background: #041018;
    color: #d5e9ef;
    selection-background-color: #0f6076;
}
QHeaderView::section {
    border: none;
    border-bottom: 1px solid #1c4254;
    padding: 4px 6px;
    background: #092331;
    color: #8bdaf1;
    font-weight: 600;
}
QTableWidget::item {
    padding: 3px 5px;
}
QPushButton {
    min-height: 28px;
    border: 1px solid #24576b;
    border-radius: 4px;
    padding: 4px 10px;
    background: #092331;
    color: #e5f6fb;
    font-weight: 600;
}
QPushButton:hover {
    border-color: #2ba7c7;
    background: #0d3142;
}
QPushButton:pressed {
    background: #123d4d;
}
QStatusBar {
    background: #040c12;
    border-top: 1px solid #153544;
    color: #9bb8c3;
}
"""

STATUS_CARD_BASE_QSS = """
QFrame {
    border: 1px solid %(border)s;
    border-radius: 6px;
    background: %(background)s;
}
QLabel {
    background: transparent;
}
"""

STATUS_TONES = {
    "idle": ("#1b3f4d", "#071923"),
    "ok": ("#159a7e", "#071e1f"),
    "warn": ("#c18521", "#201807"),
    "danger": ("#ba332c", "#220b0b"),
    "active": ("#1e8fb0", "#061d29"),
}


def monitor_button_style(active: bool) -> str:
    """Return the prominent start/stop button style."""
    if active:
        return (
            "QPushButton { background: #b52b28; color: #fff3ef; "
            "border: 1px solid #ff5b50; border-radius: 5px; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { background: #ce332f; }"
        )
    return (
        "QPushButton { background: #0d5f75; color: #edfbff; "
        "border: 1px solid #23b7d8; border-radius: 5px; "
        "font-size: 16px; font-weight: bold; }"
        "QPushButton:hover { background: #11718a; }"
    )


def status_card_style(tone: str) -> str:
    """Return a status card stylesheet for a named tone."""
    border, background = STATUS_TONES.get(tone, STATUS_TONES["idle"])
    return STATUS_CARD_BASE_QSS % {"border": border, "background": background}
