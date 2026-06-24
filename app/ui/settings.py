"""Settings panel: whitelist management, scan interval, window keyword."""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.models.whitelist import Whitelist


class SettingsPanel(QWidget):
    """Left-side control panel with whitelist editor and scan config."""

    whitelist_changed = pyqtSignal()

    def __init__(self, whitelist: Whitelist, parent=None):
        super().__init__(parent)
        self._whitelist = whitelist

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # --- Whitelist group ---
        wl_group = QGroupBox("白名单管理")
        wl_layout = QVBoxLayout(wl_group)

        self._wl_list = QListWidget()
        self._refresh_wl_list()
        wl_layout.addWidget(self._wl_list)

        # Buttons row
        btn_row = QHBoxLayout()
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add_entry)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._remove_entry)
        import_btn = QPushButton("导入")
        import_btn.clicked.connect(self._import_file)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addWidget(import_btn)
        wl_layout.addLayout(btn_row)

        layout.addWidget(wl_group)

        # --- Scan config group ---
        cfg_group = QGroupBox("扫描设置")
        cfg_layout = QVBoxLayout(cfg_group)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("扫描间隔 (秒):"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 10)
        self._interval_spin.setValue(2)
        self._interval_spin.setSuffix(" 秒")
        interval_row.addWidget(self._interval_spin)
        interval_row.addStretch()
        cfg_layout.addLayout(interval_row)

        keyword_row = QHBoxLayout()
        keyword_row.addWidget(QLabel("窗口关键词:"))
        self._keyword_edit = QLineEdit("EVE -")
        keyword_row.addWidget(self._keyword_edit)
        cfg_layout.addLayout(keyword_row)

        layout.addWidget(cfg_group)
        layout.addStretch()

    def _refresh_wl_list(self):
        """Reload the list widget from the whitelist model."""
        self._wl_list.clear()
        for name in sorted(self._whitelist.get_all()):
            self._wl_list.addItem(name)

    def _add_entry(self):
        from PyQt6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "添加白名单", "玩家/军团名 (支持 * 通配符):")
        if ok and name.strip():
            self._whitelist.add(name.strip())
            self._refresh_wl_list()
            self.whitelist_changed.emit()

    def _remove_entry(self):
        item = self._wl_list.currentItem()
        if item:
            self._whitelist.remove(item.text())
            self._refresh_wl_list()
            self.whitelist_changed.emit()

    def _import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入白名单", "", "文本文件 (*.txt);;所有文件 (*)"
        )
        if path:
            count = self._whitelist.import_from_file(path)
            self._refresh_wl_list()
            self.whitelist_changed.emit()
            QMessageBox.information(self, "导入完成", f"已导入 {count} 个条目。")

    def get_interval(self) -> float:
        return float(self._interval_spin.value())

    def get_keyword(self) -> str:
        return self._keyword_edit.text().strip()
