"""Background worker thread for the monitor loop."""

import hashlib
import logging
import threading
import time
from typing import Optional

from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal

from app.engine.capturer import (
    BackgroundCaptureUnavailable,
    Capturer,
    TargetWindowClosed,
)
from app.engine.hostile_icons import find_hostile_icons
from app.engine.ocr import OCREngine
from app.engine.ocr_scheduler import OCRRequestSuperseded

logger = logging.getLogger(__name__)


def build_scan_status(ocr_results: list[tuple[str, float]]) -> str:
    """Build monitor status text for the complete raw OCR upload."""
    return f"OCR 识别完成: {len(ocr_results)} 个文本候选，已进入上报队列"


def build_ocr_snapshot_names(ocr_results: list[tuple[str, float]]) -> list[str]:
    """Return every OCR text result without client-side filtering."""
    return [str(text) for text, _confidence in ocr_results]


class MonitorWorker(QThread):
    """Runs capture -> OCR -> report-only snapshot publishing on a timer."""

    CAPTURE_FAILURE_THRESHOLD = 3

    status_update = pyqtSignal(str)       # human-readable status message
    scan_complete = pyqtSignal(int)       # total scan count
    ocr_snapshot = pyqtSignal(list, int)  # names, verified hostile-icon count
    ocr_evidence_snapshot = pyqtSignal(list, int, object)
    hostile_detected = pyqtSignal(int)    # emitted when the visible count changes
    connection_lost = pyqtSignal(str)     # capture/window connection is offline
    connection_restored = pyqtSignal()    # capture resumed after an offline state

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
        self._burst_scans_remaining = 0
        self._running = False
        self._region: Optional[dict] = None  # {x, y, w, h}
        self._window: Optional[dict] = None  # {hwnd, title, w, h}
        self._ocr_request_key: str | None = None
        self._ocr_enabled = True
        self._presence_refresh_requested = threading.Event()
        self._capture_failure_count = 0
        self._capture_lost = False

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

    def _recognize_with_boxes(self, image, progress=None, *, priority: int = 0):
        """Run internal OCR with optional geometry; upload only text names."""
        recognize_latest = getattr(self._ocr, "recognize_with_boxes_latest", None)
        if callable(recognize_latest) and self._ocr_request_key:
            return recognize_latest(
                image,
                progress=progress,
                request_key=self._ocr_request_key,
                priority=priority,
            )
        recognize = getattr(self._ocr, "recognize_with_boxes", None)
        if callable(recognize):
            return recognize(image, progress=progress)
        return []

    def set_interval(self, seconds: float) -> None:
        """Set the delay between scans (1-10 seconds)."""
        self._interval = max(1.0, min(10.0, float(seconds)))
        self._active_interval = self._interval

    def set_scan_offset(self, seconds: float) -> None:
        """Delay the first scan so multiple windows do not capture together."""
        self._scan_offset = max(0.0, float(seconds))

    def set_ocr_enabled(self, enabled: bool) -> None:
        """Enable or disable name OCR without stopping visual threat detection."""
        self._ocr_enabled = bool(enabled)

    def request_presence_refresh(self) -> None:
        """Re-publish the current visual count after the monitored system changes."""
        self._presence_refresh_requested.set()

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

    def _mark_capture_success(self) -> None:
        """Reset capture failures and announce recovery after a real outage."""
        self._capture_failure_count = 0
        if self._capture_lost:
            self._capture_lost = False
            self.connection_restored.emit()

    def _mark_capture_failure(self, message: str, *, definitive: bool = False) -> None:
        """Emit one loss transition after a closed window or repeated failures."""
        self._capture_failure_count += 1
        if not definitive and self._capture_failure_count < self.CAPTURE_FAILURE_THRESHOLD:
            return
        if self._capture_lost:
            return
        self._capture_lost = True
        self.connection_lost.emit(str(message or "后台画面不可用"))

    def run(self) -> None:
        """Main loop.  Runs until :meth:`stop` is called."""
        self._running = True
        scan_count = 0
        ocr_ready = False  # track whether OCR has been lazy-initialised
        previous_hostile_count: int | None = None
        last_health_status_at = time.monotonic()
        previous_hostile_rows_fingerprint: bytes | None = None
        ocr_retry_remaining = 0
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
                    self._mark_capture_success()
                    if self._stop_requested():
                        break

                    # 2. Publish visual evidence first. OCR is optional enrichment.
                    hostile_icons = find_hostile_icons(img)
                    hostile_count = len(hostile_icons)
                    force_presence_refresh = self._presence_refresh_requested.is_set()
                    if force_presence_refresh:
                        self._presence_refresh_requested.clear()
                    count_changed = (
                        force_presence_refresh
                        or hostile_count != previous_hostile_count
                    )
                    if count_changed:
                        self.hostile_detected.emit(hostile_count)
                        self._burst_scans_remaining = 2 if hostile_count > 0 else 0
                    previous_hostile_count = hostile_count

                    # OCR receives the full member list. The fingerprint uses
                    # the name-column view so friendly roster changes are
                    # noticed without reacting to distance/type animations.
                    rows_fingerprint = _image_fingerprint(img)
                    rows_changed = rows_fingerprint != previous_hostile_rows_fingerprint
                    previous_hostile_rows_fingerprint = rows_fingerprint
                    if hostile_count == 0:
                        ocr_retry_remaining = 0
                    elif count_changed or rows_changed:
                        # Allow the list to finish repainting before giving up on
                        # a transiently incomplete OCR frame.
                        ocr_retry_remaining = 3

                    should_run_ocr = (
                        self._ocr_enabled
                        and hostile_count > 0
                        and (count_changed or rows_changed or ocr_retry_remaining > 0)
                    )
                    if not should_run_ocr:
                        scan_count += 1
                        self.scan_complete.emit(scan_count)
                        if count_changed:
                            if not self._ocr_enabled and hostile_count > 0:
                                self.status_update.emit(
                                    f"已上报 {hostile_count} 个敌对图标，OCR 已关闭"
                                )
                            elif hostile_count == 0:
                                self.status_update.emit("未检测到敌对图标")
                        elif time.monotonic() - last_health_status_at >= 15.0:
                            if hostile_count > 0:
                                self.status_update.emit(
                                    "持续监测中: "
                                    f"{hostile_count} 个敌对图标，数量未变化，"
                                    "OCR 未重复执行"
                                )
                            else:
                                self.status_update.emit(
                                    "持续监测中: 未检测到敌对图标"
                                )
                            last_health_status_at = time.monotonic()
                        self._active_interval = (
                            max(1.0, self._interval * 0.5)
                            if self._burst_scans_remaining > 0
                            else self._interval
                        )
                        self._burst_scans_remaining = max(
                            0,
                            self._burst_scans_remaining - 1,
                        )
                        self._wait_for_next_scan()
                        continue

                    progress = None if ocr_ready else self.status_update.emit
                    ocr_retry_remaining = max(0, ocr_retry_remaining - 1)
                    supports_full_frame_ocr = any(
                        callable(getattr(self._ocr, method, None))
                        for method in (
                            "recognize_with_boxes",
                            "recognize_with_boxes_latest",
                        )
                    )
                    if supports_full_frame_ocr:
                        full_ocr_results = self._recognize_with_boxes(
                            img,
                            progress=progress,
                            priority=10,
                        )
                        ocr_results = [
                            (text, confidence)
                            for text, confidence, _bounds in full_ocr_results
                        ]
                        logger.info(
                            "Full-frame OCR roster published (icons=%d, candidates=%d)",
                            hostile_count,
                            len(full_ocr_results),
                        )
                    else:
                        # Legacy backends have no geometry API. OCR the complete
                        # captured member list so friendly names are retained too.
                        ocr_results = self._recognize(
                            img,
                            progress=progress,
                            priority=10,
                        )
                    if ocr_results:
                        self.ocr_snapshot.emit(
                            build_ocr_snapshot_names(ocr_results),
                            hostile_count,
                        )
                        ocr_retry_remaining = 0
                    ocr_ready = True
                    if self._stop_requested():
                        break

                    # 3. Name OCR is best-effort; visual count has already been sent.
                    scan_count += 1
                    self.scan_complete.emit(scan_count)
                    if ocr_results:
                        self.status_update.emit(build_scan_status(ocr_results))

                    if self._burst_scans_remaining > 0:
                        self._active_interval = max(1.0, self._interval * 0.5)
                        self._burst_scans_remaining = max(
                            0,
                            self._burst_scans_remaining - 1,
                        )
                    else:
                        self._active_interval = self._interval

                except OCRRequestSuperseded:
                    logger.debug("Discarded superseded OCR frame")
                    ocr_retry_remaining = max(1, ocr_retry_remaining)
                    self.status_update.emit("OCR 已跳过过期帧")
                except TargetWindowClosed:
                    self._mark_capture_failure(
                        "EVE 窗口已关闭，等待自动重连",
                        definitive=True,
                    )
                    logger.info("Target EVE window closed; waiting for monitor reconnect")
                    self.status_update.emit("EVE 窗口已关闭，等待自动重连")
                    break
                except BackgroundCaptureUnavailable:
                    self._mark_capture_failure("后台画面连续不可用")
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


def _image_fingerprint(image) -> bytes:
    """Return a cheap fingerprint for an OCR input image.

    The monitored member-list image is modest in size, so hashing a reduced
    name-column view is inexpensive and detects same-count roster changes.
    The fallback keeps worker tests and alternate image providers compatible.
    """
    if image is None:
        return b""
    try:
        grayscale = image.convert("L")
        width, height = grayscale.size
        # Focus on the name column. Overview distance and type/size columns can
        # update while the hostile roster itself remains unchanged.
        name_left = min(width - 1, max(0, int(round(width * 0.12))))
        name_right = max(name_left + 1, int(round(width * 0.62)))
        grayscale = grayscale.crop((name_left, 0, min(width, name_right), height))
        # Icon detection can move the extracted crop by a pixel or two between
        # frames. Remove the blank border before resizing so that this geometry
        # jitter does not look like a roster change. The name-column crop keeps
        # changing distance/type columns out of the fingerprint.
        binary = grayscale.point(lambda value: 255 if value >= 96 else 0)
        content_box = binary.getbbox()
        if content_box is not None:
            normalized = binary.crop(content_box)
        else:
            normalized = binary
        if normalized.width > 96 or normalized.height > 192:
            normalized.thumbnail((96, 192))
        reduced = Image.new("1", (96, 192))
        reduced.paste(normalized, (0, 0))
        return hashlib.blake2b(reduced.tobytes(), digest_size=12).digest()
    except (AttributeError, TypeError, ValueError):
        return hashlib.blake2b(repr(image).encode("utf-8"), digest_size=12).digest()
