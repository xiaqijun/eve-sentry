"""Transparent fullscreen overlay for mouse drag-to-select region.

Provides a frameless, translucent window that spans all monitors.
The user presses and drags the mouse to draw a selection rectangle,
then releases to confirm. Escape cancels the operation.
"""

import logging

from PyQt6.QtCore import Qt, pyqtSignal, QRect
from PyQt6.QtGui import QColor, QKeyEvent, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

logger = logging.getLogger(__name__)


class RegionSelector(QWidget):
    """Fullscreen semi-transparent overlay for drag-to-select.

    Covers all monitors with a dark translucent background.  The user
    presses the left mouse button, drags to draw a rectangle, and
    releases to confirm the region.  Pressing Escape cancels without
    emitting a signal.
    """

    region_selected = pyqtSignal(int, int, int, int)
    """Emitted on mouse release: (x, y, w, h) in virtual-screen coordinates."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Mouse drag state
        self._start_point = None  # QPoint | None
        self._end_point = None  # QPoint | None
        self._is_dragging = False

        # Cover every monitor (virtual desktop)
        self._setup_geometry()

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _setup_geometry(self) -> None:
        """Resize and position the widget over the entire virtual desktop."""
        geom = QRect()
        for screen in QApplication.screens():
            geom = geom.united(screen.geometry())
        self.setGeometry(geom)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw the semi-transparent overlay and the selection rectangle."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Fill entire widget with a dark semi-transparent overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

        # 2. If the user is dragging, cut a transparent hole where the
        #    selection rectangle is, then draw a coloured border.
        if self._is_dragging and self._start_point is not None:
            rect = self._normalized_rect()

            # Clear the area inside the selection (makes it transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear
            )
            painter.fillRect(rect, QColor(0, 0, 0, 0))
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )

            # Draw a red border around the selection
            painter.setPen(QPen(QColor(255, 50, 50, 255), 2))
            painter.drawRect(rect)

    def _normalized_rect(self) -> QRect:
        """Return a non-negative-width/height rect from start/end points."""
        x1 = self._start_point.x()
        y1 = self._start_point.y()
        x2 = self._end_point.x()
        y2 = self._end_point.y()
        return QRect(
            min(x1, x2),
            min(y1, y2),
            abs(x2 - x1),
            abs(y2 - y1),
        )

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_point = event.position().toPoint()
            self._end_point = self._start_point
            self._is_dragging = True
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._is_dragging:
            self._end_point = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._is_dragging:
            self._is_dragging = False
            rect = self._normalized_rect()
            if rect.width() > 0 and rect.height() > 0:
                self.region_selected.emit(
                    rect.x(), rect.y(), rect.width(), rect.height()
                )
            self.close()

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)
