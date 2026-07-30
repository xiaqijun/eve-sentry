"""EVE Sentry — EVE Online hostile player early warning system."""

import ctypes
import logging
import os
import sys

from app.diagnostics import configure_client_logging

# Skip PaddleOCR's slow connectivity check and unreachable huggingface.
# Must be set before any PaddleOCR import.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")


def configure_windows_dpi_awareness() -> None:
    """Use physical per-monitor coordinates before Qt creates any windows."""
    if sys.platform != "win32":
        return
    try:
        per_monitor_v2 = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(per_monitor_v2):
            return
    except (AttributeError, OSError, ValueError):
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError, ValueError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError, ValueError):
        pass


configure_windows_dpi_awareness()

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from app.ui.main_window import MainWindow
from app.single_instance import SingleInstanceGuard
from app.version import current_version


def main():
    configure_client_logging(logging.INFO)

    health_marker = ""
    if "--update-health-marker" in sys.argv:
        marker_index = sys.argv.index("--update-health-marker")
        if marker_index + 1 < len(sys.argv):
            health_marker = sys.argv[marker_index + 1]
            del sys.argv[marker_index:marker_index + 2]

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("EVE Sentry")
    app.setOrganizationName("EveSentry")
    app.setApplicationVersion(current_version())

    instance = SingleInstanceGuard("EveSentry-Monitor", parent=app)
    if not instance.acquire():
        return 0

    window = MainWindow()
    instance.activate_requested.connect(window.activate_window)
    app.aboutToQuit.connect(instance.close)
    if health_marker:
        try:
            from pathlib import Path

            Path(health_marker).write_text("ok", encoding="ascii")
        except OSError:
            logging.getLogger(__name__).exception(
                "Could not write update startup health marker"
            )
    if window.should_start_minimized():
        window.hide()
    else:
        window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
