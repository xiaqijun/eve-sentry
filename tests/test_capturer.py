from unittest.mock import patch, MagicMock
from PIL import Image
from app.engine.capturer import Capturer


class TestFindEveWindow:
    def make_windows(self, windows):
        """windows: list of (hwnd, title, rect) tuples.
        rect = (left, top, right, bottom) — window rect.
        client_rect = (0, 0, w, h)
        """
        hwnds = []
        titles = {}
        rects = {}
        client_rects = {}
        for hwnd, title, rect in windows:
            hwnds.append(hwnd)
            titles[hwnd] = title
            rects[hwnd] = rect
            # client_rect = (0, 0, width, height) where w,h derived from rect
            client_rects[hwnd] = (0, 0, rect[2] - rect[0], rect[3] - rect[1])

        def mock_enum_windows(callback, param):
            for hwnd in hwnds:
                callback(hwnd, param)

        def mock_get_window_text(h):
            return titles.get(h, "")

        def mock_get_window_rect(h):
            return rects.get(h, (0, 0, 0, 0))

        def mock_get_client_rect(h):
            return client_rects.get(h, (0, 0, 0, 0))

        def mock_client_to_screen(h, point):
            # point is (0, 0) tuple → return window's top-left in screen coords
            wr = rects.get(h, (0, 0, 0, 0))
            return (wr[0], wr[1])

        return (
            mock_enum_windows,
            mock_get_window_text,
            mock_get_window_rect,
            mock_get_client_rect,
            mock_client_to_screen,
        )

    @patch("app.engine.capturer.win32gui")
    def test_find_by_title_keyword(self, mock_win32gui):
        windows = [
            (1, "Chrome", (0, 0, 500, 400)),
            (2, "EVE - MyCharacter", (100, 200, 900, 800)),
            (3, "Notepad", (0, 400, 300, 600)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetWindowRect = gwr
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.find_eve_window()

        assert result is not None
        assert result["title"] == "EVE - MyCharacter"
        assert result["w"] == 800  # client width = window width
        assert result["h"] == 600  # client height = window height

    @patch("app.engine.capturer.win32gui")
    def test_no_eve_window_returns_none(self, mock_win32gui):
        windows = [
            (1, "Chrome", (0, 0, 500, 400)),
            (2, "Notepad", (0, 400, 300, 600)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.find_eve_window()
        assert result is None

    @patch("app.engine.capturer.win32gui")
    def test_custom_keyword(self, mock_win32gui):
        windows = [
            (1, "My App - Game", (0, 0, 600, 500)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetWindowRect = gwr
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.find_eve_window(keyword="My App")
        assert result is not None
        assert result["title"] == "My App - Game"


class TestScreenshot:
    @patch("app.engine.capturer.ImageGrab")
    def test_screenshot_calls_grab_with_correct_bbox(self, mock_grab):
        mock_img = MagicMock(spec=Image.Image)
        mock_grab.grab.return_value = mock_img

        c = Capturer()
        result = c.screenshot(100, 200, 300, 400)

        mock_grab.grab.assert_called_once_with(
            bbox=(100, 200, 400, 600), all_screens=True
        )
        assert result is mock_img
