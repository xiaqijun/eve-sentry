"""Screen capture and window detection for EVE Online."""

from typing import Optional

import win32gui
from PIL import Image, ImageGrab


class Capturer:
    """Handles window detection and screen region capture."""

    def find_eve_window(self, keyword: str = "EVE -") -> Optional[dict]:
        """Find the first window whose title contains ``keyword``.

        Returns:
            dict with keys title, x, y, w, h (client-area screen coords),
            or None if no matching window is found.
        """
        result = None

        def callback(hwnd, results):
            title = win32gui.GetWindowText(hwnd)
            if keyword.lower() in title.lower():
                # Convert client rect to screen coords
                client_rect = win32gui.GetClientRect(hwnd)
                pt = win32gui.ClientToScreen(hwnd, (0, 0))
                results.append({
                    "title": title,
                    "x": pt[0],
                    "y": pt[1],
                    "w": client_rect[2],
                    "h": client_rect[3],
                })
            return True

        results = []
        win32gui.EnumWindows(callback, results)
        if results:
            return results[0]
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
