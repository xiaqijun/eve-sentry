"""Overlay positioned over the EVE window for region selection."""

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QPainter, QPen
from PyQt6.QtWidgets import QWidget


def region_selector_overlay_color() -> QColor:
    """Return the translucent dimming color used by the selector overlay."""
    return QColor(20, 20, 30, 95)


def region_selector_hint_lines(title: str) -> list[str]:
    """Return the on-screen instructions for selecting a member-list region."""
    clean_title = title.strip() or "当前 EVE 窗口"
    return [
        f"正在选择窗口: {clean_title}",
        "请拖拽框选该窗口内的成员列表区域",
        "松开鼠标确认, 按 Esc 取消",
    ]


class RegionSelector(QWidget):
    """Semi-transparent overlay used to drag-select a screen region."""

    region_selected = pyqtSignal(int, int, int, int)
    selector_closed = pyqtSignal()

    def __init__(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(x, y, w, h)

        self._target_title = title
        self._start = None
        self._end = None
        self._dragging = False

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), region_selector_overlay_color())

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

        lines = region_selector_hint_lines(self._target_title)
        metrics = painter.fontMetrics()
        text_width = max(metrics.horizontalAdvance(line) for line in lines)
        line_height = metrics.height()
        panel_width = text_width + 32
        panel_height = line_height * len(lines) + 24
        panel_x = int((self.width() - panel_width) / 2)
        panel = QRect(panel_x, 18, panel_width, panel_height)
        painter.setPen(QPen(QColor(0, 220, 255, 120), 1))
        painter.setBrush(QColor(4, 12, 18, 220))
        painter.drawRoundedRect(panel, 6, 6)
        painter.setPen(QColor(245, 252, 255, 235))
        text_x = panel.left() + 16
        baseline = panel.top() + 18 + metrics.ascent()
        for index, line in enumerate(lines):
            painter.drawText(text_x, baseline + index * line_height, line)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            point = self._clamp_global_point(event.globalPosition().toPoint())
            self._start = point
            self._end = point
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._end = self._clamp_global_point(event.globalPosition().toPoint())
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

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        try:
            self.grabMouse()
            self.grabKeyboard()
        except RuntimeError:
            pass

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.releaseMouse()
            self.releaseKeyboard()
        except RuntimeError:
            pass
        self.selector_closed.emit()
        super().closeEvent(event)

    def _clamp_global_point(self, point):
        geometry = self.geometry()
        x = max(geometry.left(), min(point.x(), geometry.right()))
        y = max(geometry.top(), min(point.y(), geometry.bottom()))
        point.setX(x)
        point.setY(y)
        return point
