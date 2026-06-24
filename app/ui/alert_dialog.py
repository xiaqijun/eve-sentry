"""Modal alert dialog shown when threats are detected."""

from pathlib import Path

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtMultimedia import QSoundEffect
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class AlertDialog(QDialog):
    """Modal popup listing detected hostile player names."""

    def __init__(self, threats: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚠ 威胁预警！")
        self.setMinimumSize(300, 200)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header
        header = QLabel(f"发现 {len(threats)} 个敌对目标")
        header.setStyleSheet("font-size: 16px; font-weight: bold; color: #cc0000;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        # Threat list
        list_widget = QListWidget()
        list_widget.setStyleSheet(
            "QListWidget { border: 1px solid #cc0000; border-radius: 4px; "
            "background: #fff8f8; font-size: 14px; }"
        )
        for name in threats:
            item = QListWidgetItem(f"\U0001f6a8  {name}")
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        # OK button
        btn = QPushButton("确认")
        btn.setMinimumHeight(36)
        btn.setStyleSheet(
            "QPushButton { background: #cc0000; color: white; border-radius: 4px; "
            "font-size: 14px; font-weight: bold; }"
            "QPushButton:hover { background: #dd2222; }"
        )
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)

        # Play alert sound
        self._play_sound()

    def _play_sound(self) -> None:
        """Play the alert wav file if it exists."""
        sound_path = Path(__file__).parent.parent.parent / "resources" / "alert.wav"
        if sound_path.exists():
            try:
                self._sound = QSoundEffect()
                self._sound.setSource(QUrl.fromLocalFile(str(sound_path.resolve())))
                self._sound.setVolume(1.0)
                self._sound.play()
            except Exception:
                pass  # Sound is non-critical
