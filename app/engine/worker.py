"""Background worker thread for the monitor loop."""

import logging
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from app.engine.capturer import Capturer
from app.engine.detector import Detector
from app.engine.ocr import OCREngine

logger = logging.getLogger(__name__)


class MonitorWorker(QThread):
    """Runs capture -> OCR -> detect on a timer in a background thread."""

    threat_detected = pyqtSignal(list)   # list[str] -- new threat names
    status_update = pyqtSignal(str)       # human-readable status message
    scan_complete = pyqtSignal(int)       # total scan count

    def __init__(
        self,
        capturer: Capturer,
        ocr: OCREngine,
        detector: Detector,
        parent=None,
    ):
        super().__init__(parent)
        self._capturer = capturer
        self._ocr = ocr
        self._detector = detector
        self._interval = 2.0           # seconds between scans
        self._running = False
        self._region: Optional[dict] = None  # {x, y, w, h}

    def set_region(self, x: int, y: int, w: int, h: int) -> None:
        """Set the screen region to capture."""
        self._region = {"x": x, "y": y, "w": w, "h": h}

    def set_interval(self, seconds: float) -> None:
        """Set the delay between scans (1-10 seconds)."""
        self._interval = max(1.0, min(10.0, float(seconds)))

    def stop(self) -> None:
        """Request the loop to stop at the next iteration."""
        self._running = False

    def run(self) -> None:
        """Main loop.  Runs until :meth:`stop` is called."""
        self._running = True
        scan_count = 0

        self.status_update.emit("监控已启动")

        while self._running:
            if self._region is None:
                self.status_update.emit("未设置截图区域")
                self.msleep(500)
                continue

            try:
                # 1. Capture
                r = self._region
                img = self._capturer.screenshot(r["x"], r["y"], r["w"], r["h"])

                # 2. OCR
                ocr_results = self._ocr.recognize(img)

                # 3. Detect
                threats = self._detector.check(ocr_results)

                scan_count += 1
                self.scan_complete.emit(scan_count)

                if threats:
                    names = ", ".join(threats)
                    self.threat_detected.emit(threats)
                    self.status_update.emit(f"发现威胁: {names}")
                else:
                    self.status_update.emit("无威胁")

            except Exception:
                logger.exception("Scan cycle failed")
                self.status_update.emit("扫描出错，已跳过当前帧")

            # Wait between scans
            self.msleep(int(self._interval * 1000))
