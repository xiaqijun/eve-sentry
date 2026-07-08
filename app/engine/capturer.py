"""Screen capture and window detection for EVE Online.

Uses *zbl* (Windows.Graphics.Capture via D3D11) for background capture —
works even when EVE is occluded or minimised.
"""

import logging
import threading
from typing import Optional

import psutil
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)


class Capturer:
    """Window detection + background-capable screen capture."""

    def __init__(self):
        self._hwnd: int | None = None
        self._bg_capture = None  # zbl Capture instance, created lazily
        self._bg_capture_started = False
        self._bg_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Window detection
    # ------------------------------------------------------------------

    def get_window_info(self, hwnd: int) -> Optional[dict]:
        """Return client-area screen coordinates for a valid, non-minimized hwnd."""
        return self._build_window_info(hwnd)

    def list_eve_windows(self, keyword: str = "EVE -") -> list[dict]:
        """Return ALL EVE windows, sorted by title (most recently active first).

        Each dict has keys: title, hwnd, x, y, w, h.
        """
        results: list[dict] = []

        def title_callback(hwnd, _):
            title = win32gui.GetWindowText(hwnd)
            if title.lower().startswith(keyword.lower()):
                info = self._build_window_info(hwnd)
                if info is not None and not any(item["hwnd"] == hwnd for item in results):
                    results.append(info)
            return True

        win32gui.EnumWindows(title_callback, None)

        def process_callback(hwnd, _):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if "exefile" in psutil.Process(pid).name().lower():
                    info = self._build_window_info(hwnd)
                    if info is not None and not any(item["hwnd"] == hwnd for item in results):
                        results.append(info)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(process_callback, None)

        # Sort: prefer larger windows (real client > thumbnail), then by name
        results.sort(key=lambda i: (-i["w"] * i["h"], i["title"]))
        return results

    def _build_window_info(self, hwnd: int) -> Optional[dict]:
        """Build window metadata, skipping minimized or zero-sized clients."""
        if not win32gui.IsWindow(hwnd):
            return None

        client_rect = win32gui.GetClientRect(hwnd)
        width = client_rect[2]
        height = client_rect[3]
        title = win32gui.GetWindowText(hwnd)
        if width <= 0 or height <= 0:
            logger.info(
                "Skipping zero-sized EVE window hwnd=%d title=%r",
                hwnd,
                title,
            )
            return None

        pt = win32gui.ClientToScreen(hwnd, (0, 0))
        return {
            "title": title,
            "hwnd": hwnd,
            "x": pt[0],
            "y": pt[1],
            "w": width,
            "h": height,
        }

    def activate_window(self, hwnd: int) -> bool:
        """Restore and raise a window so the region overlay shows the target."""
        if not win32gui.IsWindow(hwnd):
            return False
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            logger.warning("Failed to activate window hwnd=%d", hwnd, exc_info=True)
            return False

    def select_window(
        self,
        hwnd: int,
        title: str = "",
        w: int = 0,
        h: int = 0,
        start_capture: bool = True,
    ) -> None:
        """Set the target window for background capture."""
        with self._bg_lock:
            self._close_unlocked()  # release old capture if any
            self._hwnd = hwnd
            if not start_capture:
                logger.info("Selected window without capture: %s (%dx%d)", title, w, h)
                return
            # Pre-create and start the zbl capture session.
            try:
                from zbl import Capture
                self._bg_capture = Capture(window_handle=hwnd)
                self._bg_capture.__enter__()
                self._bg_capture_started = True
                logger.info("zbl capture ready for hwnd=%d", hwnd)
            except Exception:
                logger.warning("zbl capture setup failed for hwnd=%d", hwnd, exc_info=True)
                self._bg_capture = None
                self._bg_capture_started = False
        logger.info("Selected window: %s (%dx%d)", title, w, h)

    def find_eve_window(self, keyword: str = "EVE -") -> Optional[dict]:
        """Return the first EVE window found (use ``list_eve_windows`` for all)."""
        windows = self.list_eve_windows(keyword)
        if windows:
            win = windows[0]
            self.select_window(win["hwnd"], win["title"], win["w"], win["h"])
            return win
        logger.info("No EVE window found matching '%s'", keyword)
        return None

    # ------------------------------------------------------------------
    # Region helper
    # ------------------------------------------------------------------

    def get_member_list_region(self, window: dict, ratio: float = 0.25,
                               margin: int = 4) -> dict:
        """Right-side member-list sub-region within the EVE window."""
        list_w = max(80, int(window["w"] * ratio))
        return {
            "x": window["x"] + window["w"] - list_w - margin,
            "y": window["y"] + margin,
            "w": list_w,
            "h": window["h"] - margin * 2,
        }

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def screenshot(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """Capture a region — background-capable when EVE hwnd is known."""
        with self._bg_lock:
            hwnd = self._hwnd
            if hwnd and win32gui.IsWindow(hwnd):
                result = self._capture_zbl_unlocked(x, y, w, h)
                if result is not None:
                    if not getattr(self, "_zbl_logged", False):
                        logger.info("Using zbl background capture for hwnd=%d", hwnd)
                        self._zbl_logged = True
                    return result
                else:
                    logger.debug("zbl returned None, falling back to ImageGrab")
                    if not getattr(self, "_fb_logged", False):
                        logger.warning("zbl capture failed — falling back to screen grab")
                        self._fb_logged = True
        # Fallback to screen grab
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)

    def _capture_zbl_unlocked(self, screen_x, screen_y, w, h) -> Optional[Image.Image]:
        """Capture via zbl (WinRT), cropped to the requested sub-region."""
        cap = self._bg_capture
        if cap is None:
            return None
        try:
            import time as _time

            # Poll for a frame (non-blocking)
            frame = None
            for _ in range(30):  # max ~0.3 s
                frame = cap.try_grab()
                if frame is not None:
                    break
                _time.sleep(0.01)
            if frame is None:
                return None

            # zbl returns the FULL window; compute offsets in client coords
            client_pt = win32gui.ClientToScreen(self._hwnd, (0, 0))
            ox = screen_x - client_pt[0]
            oy = screen_y - client_pt[1]

            # Safety clamp
            fh, fw = frame.shape[:2]
            ox = max(0, min(ox, fw - 1))
            oy = max(0, min(oy, fh - 1))
            cw = min(w, fw - ox)
            ch = min(h, fh - oy)

            crop = frame[oy:oy + ch, ox:ox + cw]
            # zbl returns BGRA — convert to RGB
            return Image.fromarray(crop[:, :, :3][:, :, ::-1], "RGB")
        except Exception:
            logger.debug("zbl capture failed, falling back", exc_info=True)
            return None
        except BaseException as exc:
            if exc.__class__.__name__ == "PanicException":
                logger.warning("zbl capture panicked; falling back to screen grab")
                return None
            raise

    def close(self) -> None:
        """Release the background capture session."""
        with self._bg_lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        """Release the background capture session while holding ``_bg_lock``."""
        try:
            if self._bg_capture is not None and self._bg_capture_started:
                self._bg_capture.__exit__(None, None, None)
        except Exception:
            pass
        self._bg_capture = None
        self._bg_capture_started = False
        self._hwnd = None
        self._zbl_logged = False
        self._fb_logged = False
