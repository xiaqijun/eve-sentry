"""EVE Sentry — EVE Online hostile player early warning system."""

import os
import logging
import sys

# Skip PaddleOCR's slow connectivity check and unreachable huggingface.
# Must be set before any PaddleOCR import.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from app.ui.main_window import MainWindow


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("EVE Sentry")
    app.setOrganizationName("EveSentry")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
