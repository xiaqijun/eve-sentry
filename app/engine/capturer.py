"""Screen capture and window detection for EVE Online."""

import logging
from typing import Optional

import psutil
import win32gui
import win32process
from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)


class Capturer:
    """Handles window detection and screen region capture."""

    def find_eve_window(self, keyword: str = "EVE -") -> Optional[dict]:
        """Find a window matching the title keyword, falling back to process name.

        First pass matches windows whose title contains *keyword*.
        Second pass (fallback) matches windows whose process name contains
        "exefile" (the EVE Online client).

        Returns:
            dict with keys title, x, y, w, h (client-area screen coords),
            or None if no matching window is found.
        """

        def _build_info(hwnd):
            """Build the info dict for a given window handle."""
            client_rect = win32gui.GetClientRect(hwnd)
            pt = win32gui.ClientToScreen(hwnd, (0, 0))
            return {
                "title": win32gui.GetWindowText(hwnd),
                "x": pt[0],
                "y": pt[1],
                "w": client_rect[2],
                "h": client_rect[3],
            }

        # ---- Pass 1: match by window title ----
        def title_callback(hwnd, results):
            title = win32gui.GetWindowText(hwnd)
            if keyword.lower() in title.lower():
                results.append(_build_info(hwnd))
            return True

        results = []
        win32gui.EnumWindows(title_callback, results)
        if results:
            return results[0]

        # ---- Pass 2: fallback — match by process name ("exefile.exe") ----
        def process_callback(hwnd, results):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid)
                if "exefile" in proc.name().lower():
                    results.append(_build_info(hwnd))
            except Exception:
                # Process may have ended, access denied, or invalid hwnd
                pass
            return True

        results2 = []
        win32gui.EnumWindows(process_callback, results2)
        if results2:
            logger.info("Found EVE window by process name (exefile.exe)")
            return results2[0]

        return None

    def screenshot(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """Capture a screen region.

        Args:
            x, y: top-left screen coordinates.
            w, h: width and height of the region.

        Returns:
            PIL Image of the captured region.
        """
        bbox = (x, y, x + w, y + h)
        return ImageGrab.grab(bbox=bbox, all_screens=True)
