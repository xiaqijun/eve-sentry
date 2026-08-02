"""Background worker thread for the monitor loop."""

import hashlib
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
from app.engine.ocr_scheduler import OCRRequestSuperseded

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
    hostile_detected = pyqtSignal(int)    # emitted when the visible count changes

    def __init__(
        self,
        capturer: Capturer,
        ocr: OCREngine,
        scan_offset: float = 0.0,
        parent=None,
    ):
        super().__init__(parent)
        self._capturer = capturer
        self._ocr = ocr
        self._interval = 2.0           # seconds between scans
        self._active_interval = self._interval
        self._scan_offset = max(0.0, float(scan_offset))
        self._previous_fingerprint = b""
        self._burst_scans_remaining = 0
        self._running = False
        self._region: Optional[dict] = None  # {x, y, w, h}
        self._window: Optional[dict] = None  # {hwnd, title, w, h}
        self._ocr_request_key: str | None = None

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
        self._ocr_request_key = f"window:{self._window['hwnd']}"

    def _recognize(self, image, progress=None, *, priority: int = 0):
        """Use scheduler coalescing when available, while keeping engine compatibility."""
        recognize_latest = getattr(self._ocr, "recognize_latest", None)
        if callable(recognize_latest) and self._ocr_request_key:
            return recognize_latest(
                image,
                progress=progress,
                request_key=self._ocr_request_key,
                priority=priority,
            )
        return self._ocr.recognize(image, progress=progress)

    def set_interval(self, seconds: float) -> None:
        """Set the delay between scans (1-10 seconds)."""
        self._interval = max(1.0, min(10.0, float(seconds)))
        self._active_interval = self._interval

    def set_scan_offset(self, seconds: float) -> None:
        """Delay the first scan so multiple windows do not capture together."""
        self._scan_offset = max(0.0, float(seconds))

    def stop(self) -> None:
        """Request the current scan and the monitor loop to stop."""
        self._running = False
        self.requestInterruption()

    def _stop_requested(self) -> bool:
        """Return whether shutdown was requested from the UI thread."""
        return not self._running or self.isInterruptionRequested()

    def _wait_for_next_scan(self) -> None:
        """Wait between scans while remaining responsive to shutdown."""
        remaining_ms = int(self._active_interval * 1000)
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
        red_row_mismatch_count = 0
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
            if self._scan_offset:
                self._active_interval = self._scan_offset
                self._wait_for_next_scan()
                self._active_interval = self._interval
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

                    fingerprint = _frame_fingerprint(img)
                    frame_changed = fingerprint != self._previous_fingerprint
                    self._previous_fingerprint = fingerprint
                    if frame_changed:
                        self._burst_scans_remaining = max(
                            self._burst_scans_remaining,
                            2,
                        )

                    # 2. Detect hostile icons before OCR and publish count changes.
                    hostile_icons = find_hostile_icons(img)
                    hostile_count = len(hostile_icons)
                    if hostile_count != previous_hostile_count:
                        self.hostile_detected.emit(hostile_count)
                        self._burst_scans_remaining = 4
                    previous_hostile_count = hostile_count

                    if not frame_changed and hostile_count == 0:
                        scan_count += 1
                        self.scan_complete.emit(scan_count)
                        self._active_interval = self._interval
                        self.status_update.emit("画面无变化，已跳过 OCR")
                        self._wait_for_next_scan()
                        continue

                    hostile_rows = (
                        extract_hostile_name_rows(img) if hostile_icons else None
                    )
                    publish_snapshot = True
                    fallback_deferred = False
                    if hostile_rows is None:
                        red_row_mismatch_count = 0
                        if not ocr_ready:
                            warm_up = getattr(self._ocr, "warm_up", None)
                            if callable(warm_up):
                                warm_up()
                            else:
                                # Preserve direct engine compatibility for tools/tests.
                                self._recognize(
                                    img,
                                    progress=self.status_update.emit,
                                )
                        ocr_results = []
                        names = []
                        verified_hostile_count = 0
                    else:
                        progress = None if ocr_ready else self.status_update.emit
                        hostile_ocr_results = self._recognize(
                            hostile_rows,
                            progress=progress,
                            priority=10,
                        )
                        hostile_names = build_ocr_snapshot_names(hostile_ocr_results)
                        if len(hostile_names) == hostile_count:
                            red_row_mismatch_count = 0
                            ocr_results = hostile_ocr_results
                            names = hostile_names
                            verified_hostile_count = hostile_count
                        else:
                            red_row_mismatch_count += 1
                            if red_row_mismatch_count < 2:
                                # A contact-menu overlay or list repaint can corrupt
                                # one frame. Keep the last server state until confirmed.
                                ocr_results = hostile_ocr_results
                                names = []
                                verified_hostile_count = 0
                                publish_snapshot = False
                                fallback_deferred = True
                            else:
                                # Do not assign a red marker to the wrong pilot. Fall
                                # back to the complete list after a confirmed mismatch.
                                ocr_results = self._recognize(img)
                                names = build_ocr_snapshot_names(ocr_results)
                                verified_hostile_count = 0
                    if not ocr_ready:
                        ocr_ready = True
                    if self._stop_requested():
                        break

                    # 3. Publish names and independent visual-hostility evidence.
                    if publish_snapshot:
                        self.ocr_snapshot.emit(names, verified_hostile_count)
                    scan_count += 1
                    self.scan_complete.emit(scan_count)
                    self.status_update.emit(build_scan_status(ocr_results))

                    if hostile_count > 0 or self._burst_scans_remaining > 0:
                        self._active_interval = max(0.5, self._interval * 0.5)
                        self._burst_scans_remaining = max(
                            0,
                            self._burst_scans_remaining - 1,
                        )
                    else:
                        self._active_interval = self._interval

                    if fallback_deferred:
                        self.status_update.emit(
                            "红框姓名定位失败 1/2，等待下一帧确认"
                        )
                    elif verified_hostile_count:
                        self.status_update.emit(f"敌对名单已上报: {len(names)} 个")
                    elif names and hostile_rows is not None:
                        self.status_update.emit(
                            f"红框姓名定位不可靠，已回退完整名单: {len(names)} 个"
                        )
                    elif hostile_rows is None:
                        self.status_update.emit("未检测到敌对图标")
                    else:
                        self.status_update.emit("敌对图标行未识别到姓名")

                except OCRRequestSuperseded:
                    logger.debug("Discarded superseded OCR frame")
                    self.status_update.emit("OCR 已跳过过期帧")
                except TargetWindowClosed:
                    logger.info("Target EVE window closed; waiting for monitor reconnect")
                    self.status_update.emit("EVE 窗口已关闭，等待自动重连")
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


def _frame_fingerprint(image) -> bytes:
    """Return a cheap frame fingerprint for OCR change detection."""
    reduced = image.convert("L").resize((32, 32))
    return hashlib.blake2b(reduced.tobytes(), digest_size=12).digest()
