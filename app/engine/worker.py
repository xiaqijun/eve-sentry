"""Background worker thread for the monitor loop."""

import logging
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from app.engine.capturer import (
    BackgroundCaptureUnavailable,
    Capturer,
    TargetWindowClosed,
)
from app.engine.hostile_icons import extract_hostile_name_rows, find_hostile_icons
from app.engine.ocr import OCREngine
from app.engine.ocr_names import ocr_candidate_names

logger = logging.getLogger(__name__)


def build_scan_status(ocr_results: list[tuple[str, float]]) -> str:
    """Build the monitor status text using cleaned member names, not OCR noise."""
    member_names = ocr_candidate_names(ocr_results)
    return (
        "名单识别: "
        f"{len(member_names)} 个成员 / "
        f"{len(member_names)} 个唯一 / "
        "已上报服务器"
    )


def build_ocr_snapshot_names(ocr_results: list[tuple[str, float]]) -> list[str]:
    """Return the cleaned pilot names to publish for the OCR snapshot."""
    return ocr_candidate_names(ocr_results)


class MonitorWorker(QThread):
    """Runs capture -> OCR -> report-only snapshot publishing on a timer."""

    status_update = pyqtSignal(str)       # human-readable status message
    scan_complete = pyqtSignal(int)       # total scan count
    ocr_snapshot = pyqtSignal(list, int)  # names, verified hostile-icon count
    hostile_detected = pyqtSignal(int)    # emitted when the visible count increases

    def __init__(
        self,
        capturer: Capturer,
        ocr: OCREngine,
        parent=None,
    ):
        super().__init__(parent)
        self._capturer = capturer
        self._ocr = ocr
        self._interval = 2.0           # seconds between scans
        self._running = False
        self._region: Optional[dict] = None  # {x, y, w, h}
        self._window: Optional[dict] = None  # {hwnd, title, w, h}

    def set_region(self, x: int, y: int, w: int, h: int) -> None:
        """Set the screen region to capture."""
        self._region = {"x": x, "y": y, "w": w, "h": h}

    def set_window(self, window: dict) -> None:
        """Set the EVE window used by the worker-owned capture session."""
        self._window = {
            "hwnd": window["hwnd"],
            "title": window.get("title", ""),
            "w": window.get("w", 0),
            "h": window.get("h", 0),
        }

    def set_interval(self, seconds: float) -> None:
        """Set the delay between scans (1-10 seconds)."""
        self._interval = max(1.0, min(10.0, float(seconds)))

    def stop(self) -> None:
        """Request the current scan and the monitor loop to stop."""
        self._running = False
        self.requestInterruption()

    def _stop_requested(self) -> bool:
        """Return whether shutdown was requested from the UI thread."""
        return not self._running or self.isInterruptionRequested()

    def _wait_for_next_scan(self) -> None:
        """Wait between scans while remaining responsive to shutdown."""
        remaining_ms = int(self._interval * 1000)
        while remaining_ms > 0 and not self._stop_requested():
            sleep_ms = min(100, remaining_ms)
            self.msleep(sleep_ms)
            remaining_ms -= sleep_ms

    def run(self) -> None:
        """Main loop.  Runs until :meth:`stop` is called."""
        self._running = True
        scan_count = 0
        ocr_ready = False  # track whether OCR has been lazy-initialised
        previous_hostile_count = 0
        capturer = self._capturer
        owns_capturer = False

        if self._window is not None:
            capturer = Capturer()
            capturer.select_window(
                self._window["hwnd"],
                self._window["title"],
                self._window["w"],
                self._window["h"],
            )
            owns_capturer = True

        self.status_update.emit("监控已启动")

        try:
            while not self._stop_requested():
                if self._region is None:
                    self.status_update.emit("未设置截图区域")
                    self._wait_for_next_scan()
                    continue

                try:
                    # 1. Capture
                    r = self._region
                    if r and scan_count == 0:
                        self.status_update.emit(
                            f"截图区域: ({r['x']},{r['y']}) {r['w']}×{r['h']}"
                        )
                    img = capturer.screenshot(r["x"], r["y"], r["w"], r["h"])
                    if self._stop_requested():
                        break

                    # 2. Detect hostile icons before OCR and alert on the visual edge.
                    hostile_icons = find_hostile_icons(img)
                    hostile_count = len(hostile_icons)
                    if hostile_count > previous_hostile_count:
                        self.hostile_detected.emit(hostile_count)
                    previous_hostile_count = hostile_count

                    hostile_rows = (
                        extract_hostile_name_rows(img) if hostile_icons else None
                    )
                    if hostile_rows is None:
                        if not ocr_ready:
                            # Warm the model once, but keep a no-hostile snapshot empty.
                            self._ocr.recognize(
                                img, progress=self.status_update.emit
                            )
                        ocr_results = []
                        names = []
                        verified_hostile_count = 0
                    else:
                        progress = None if ocr_ready else self.status_update.emit
                        hostile_ocr_results = self._ocr.recognize(
                            hostile_rows,
                            progress=progress,
                        )
                        hostile_names = build_ocr_snapshot_names(hostile_ocr_results)
                        if len(hostile_names) == hostile_count:
                            ocr_results = hostile_ocr_results
                            names = hostile_names
                            verified_hostile_count = hostile_count
                        else:
                            # Do not assign a red marker to the wrong pilot. Fall back
                            # to the complete list and let server-side identity rules
                            # classify it normally.
                            ocr_results = self._ocr.recognize(img)
                            names = build_ocr_snapshot_names(ocr_results)
                            verified_hostile_count = 0
                    if not ocr_ready:
                        ocr_ready = True
                    if self._stop_requested():
                        break

                    # 3. Publish names and independent visual-hostility evidence.
                    self.ocr_snapshot.emit(names, verified_hostile_count)
                    scan_count += 1
                    self.scan_complete.emit(scan_count)
                    self.status_update.emit(build_scan_status(ocr_results))

                    if verified_hostile_count:
                        self.status_update.emit(f"敌对名单已上报: {len(names)} 个")
                    elif names and hostile_rows is not None:
                        self.status_update.emit(
                            f"红框姓名定位不可靠，已回退完整名单: {len(names)} 个"
                        )
                    elif hostile_rows is None:
                        self.status_update.emit("未检测到敌对图标")
                    else:
                        self.status_update.emit("敌对图标行未识别到姓名")

                except TargetWindowClosed:
                    logger.info("Target EVE window closed; stopping monitor worker")
                    self.status_update.emit("EVE 窗口已关闭，监控已停止")
                    break
                except BackgroundCaptureUnavailable:
                    logger.debug("Background capture unavailable; skipping OCR frame")
                    self.status_update.emit("后台画面暂不可用，已跳过当前帧")
                except Exception:
                    logger.exception("Scan cycle failed")
                    self.status_update.emit("扫描出错，已跳过当前帧")

                # Wait between scans
                self._wait_for_next_scan()
        finally:
            if owns_capturer:
                capturer.close()
