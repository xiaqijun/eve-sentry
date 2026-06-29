"""Overlay positioned over the EVE window for region selection."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class RegionSelector(QWidget):
    """Semi-transparent overlay used to drag-select a screen region."""

    region_selected = pyqtSignal(int, int, int, int)
    selector_closed = pyqtSignal()

    def __init__(
        self, x: int, y: int, w: int, h: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowOpacity(0.35)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(x, y, w, h)

        self._start = None
        self._end = None
        self._dragging = False

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(20, 20, 30))

        if self._dragging and self._start is not None and self._end is not None:
            p1 = self.mapFromGlobal(self._start)
            p2 = self.mapFromGlobal(self._end)
            rx = min(p1.x(), p2.x())
            ry = min(p1.y(), p2.y())
            rw = abs(p2.x() - p1.x())
            rh = abs(p2.y() - p1.y())
            painter.setPen(QPen(QColor(0, 255, 100, 220), 2))
            painter.setBrush(QColor(0, 255, 100, 40))
            painter.drawRect(rx, ry, rw, rh)

        hint = "Drag to select the member list   |   ESC cancels"
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(hint)
        painter.setPen(QColor(255, 255, 255, 200))
        painter.drawText(int((self.width() - text_width) / 2), 30, hint)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.globalPosition().toPoint()
            self._start = point
            self._end = point
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._end = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            x1, y1 = self._start.x(), self._start.y()
            x2, y2 = self._end.x(), self._end.y()
            sx = min(x1, x2)
            sy = min(y1, y2)
            sw = abs(x2 - x1)
            sh = abs(y2 - y1)
            if sw > 10 and sh > 10:
                self.region_selected.emit(sx, sy, sw, sh)
            self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.selector_closed.emit()
        super().closeEvent(event)
