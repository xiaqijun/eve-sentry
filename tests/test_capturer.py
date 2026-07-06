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
    @patch("app.engine.capturer.win32process")
    def test_list_eve_windows_returns_all_matching_windows_sorted(
        self, mock_win32process, mock_win32gui
    ):
        windows = [
            (1, "EVE - Pilot Small", (0, 0, 640, 480)),
            (2, "EVE - Pilot Large", (100, 100, 1380, 820)),
            (3, "Notepad", (0, 0, 300, 300)),
            (4, "EVE - Pilot Zero", (50, 50, 50, 50)),
        ]
        em, gwt, _gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.list_eve_windows()

        assert [item["title"] for item in result] == [
            "EVE - Pilot Large",
            "EVE - Pilot Small",
        ]
        assert [item["hwnd"] for item in result] == [2, 1]
        assert result[0]["w"] == 1280
        assert result[0]["h"] == 720
        mock_win32process.GetWindowThreadProcessId.assert_not_called()

    @patch("app.engine.capturer.win32gui")
    @patch("app.engine.capturer.win32process")
    @patch("app.engine.capturer.psutil")
    def test_no_eve_window_returns_none(
        self, mock_psutil, mock_win32process, mock_win32gui
    ):
        windows = [
            (1, "Chrome", (0, 0, 500, 400)),
            (2, "Notepad", (0, 400, 300, 600)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        # Make the process-name fallback raise on every hwnd
        mock_win32process.GetWindowThreadProcessId.side_effect = (
            ValueError("invalid hwnd")
        )

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

    @patch("app.engine.capturer.win32gui")
    def test_get_window_info_uses_selected_hwnd(self, mock_win32gui):
        windows = [
            (7, "EVE - Selected", (20, 30, 820, 630)),
        ]
        _em, gwt, _gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.IsWindow.return_value = True
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        c = Capturer()
        result = c.get_window_info(7)

        assert result == {
            "title": "EVE - Selected",
            "hwnd": 7,
            "x": 20,
            "y": 30,
            "w": 800,
            "h": 600,
        }

    @patch("app.engine.capturer.win32gui")
    @patch("app.engine.capturer.win32process")
    @patch("app.engine.capturer.psutil")
    def test_zero_sized_eve_window_is_ignored(
        self, mock_psutil, mock_win32process, mock_win32gui
    ):
        windows = [
            (1, "EVE - Minimized", (-32000, -32000, -32000, -32000)),
        ]
        em, gwt, gwr, gcr, cts = self.make_windows(windows)
        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetWindowRect = gwr
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts
        mock_win32process.GetWindowThreadProcessId.side_effect = ValueError(
            "invalid hwnd"
        )

        c = Capturer()

        assert c.find_eve_window() is None


class TestFindEveWindowByProcess:
    def make_windows(self, windows):
        """windows: list of (hwnd, title, rect, pid, proc_name) tuples."""
        hwnds = []
        titles = {}
        rects = {}
        client_rects = {}
        pids = {}
        proc_names = {}

        for hwnd, title, rect, pid, proc_name in windows:
            hwnds.append(hwnd)
            titles[hwnd] = title
            rects[hwnd] = rect
            client_rects[hwnd] = (0, 0, rect[2] - rect[0], rect[3] - rect[1])
            pids[hwnd] = pid
            proc_names[pid] = proc_name

        def mock_enum_windows(callback, param):
            for hwnd in hwnds:
                callback(hwnd, param)

        def mock_get_window_text(h):
            return titles.get(h, "")

        def mock_get_client_rect(h):
            return client_rects.get(h, (0, 0, 0, 0))

        def mock_client_to_screen(h, point):
            wr = rects.get(h, (0, 0, 0, 0))
            return (wr[0], wr[1])

        def mock_get_window_thread_process_id(hwnd):
            return (0, pids.get(hwnd, 0))

        def mock_process(pid):
            m = MagicMock()
            m.name.return_value = proc_names.get(pid, "")
            return m

        return (
            mock_enum_windows,
            mock_get_window_text,
            mock_get_client_rect,
            mock_client_to_screen,
            mock_get_window_thread_process_id,
            mock_process,
        )

    @patch("app.engine.capturer.win32gui")
    @patch("app.engine.capturer.win32process")
    @patch("app.engine.capturer.psutil")
    def test_find_by_process_name_fallback(
        self, mock_psutil, mock_win32process, mock_win32gui
    ):
        """No title matches, but process name matches exefile.exe."""
        windows = [
            (1, "Chrome", (0, 0, 500, 400), 100, "chrome.exe"),
            (2, "EVE Launcher", (50, 50, 850, 750), 200, "exefile.exe"),
        ]
        (
            em, gwt, gcr, cts, gwtpid, mkproc
        ) = self.make_windows(windows)

        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        mock_win32process.GetWindowThreadProcessId = gwtpid
        mock_psutil.Process = mkproc

        c = Capturer()
        result = c.find_eve_window(keyword="EVE -")

        assert result is not None
        assert result["title"] == "EVE Launcher"

    @patch("app.engine.capturer.win32gui")
    @patch("app.engine.capturer.win32process")
    @patch("app.engine.capturer.psutil")
    def test_no_match_in_either_pass(
        self, mock_psutil, mock_win32process, mock_win32gui
    ):
        """Neither title nor process name matches."""
        windows = [
            (1, "Chrome", (0, 0, 500, 400), 100, "chrome.exe"),
            (2, "Notepad", (0, 400, 300, 600), 200, "notepad.exe"),
        ]
        (
            em, gwt, gcr, cts, gwtpid, mkproc
        ) = self.make_windows(windows)

        mock_win32gui.EnumWindows = em
        mock_win32gui.GetWindowText = gwt
        mock_win32gui.GetClientRect = gcr
        mock_win32gui.ClientToScreen = cts

        mock_win32process.GetWindowThreadProcessId = gwtpid
        mock_psutil.Process = mkproc

        c = Capturer()
        result = c.find_eve_window()

        assert result is None


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


class TestBackgroundCaptureLifecycle:
    @patch("app.engine.capturer.win32gui")
    def test_activate_window_restores_and_raises_target(self, mock_win32gui):
        mock_win32gui.IsWindow.return_value = True

        capturer = Capturer()

        assert capturer.activate_window(123) is True
        mock_win32gui.ShowWindow.assert_called_once()
        assert mock_win32gui.SetWindowPos.call_count == 2
        mock_win32gui.SetForegroundWindow.assert_called_once_with(123)

    @patch("app.engine.capturer.win32gui")
    def test_select_window_starts_and_close_stops_zbl_capture(self, mock_win32gui):
        fake_capture = MagicMock()
        fake_factory = MagicMock(return_value=fake_capture)
        mock_win32gui.IsWindow.return_value = False

        with patch("zbl.Capture", fake_factory):
            capturer = Capturer()
            capturer.select_window(123, "EVE - Pilot", 800, 600)

            fake_factory.assert_called_once_with(window_handle=123)
            fake_capture.__enter__.assert_called_once_with()
            assert capturer._bg_capture_started is True

            capturer.close()

            fake_capture.__exit__.assert_called_once_with(None, None, None)
            assert capturer._bg_capture_started is False

    @patch("app.engine.capturer.win32gui")
    def test_select_window_can_skip_zbl_start(self, mock_win32gui):
        fake_factory = MagicMock()

        with patch("zbl.Capture", fake_factory):
            capturer = Capturer()
            capturer.select_window(
                123,
                "EVE - Pilot",
                800,
                600,
                start_capture=False,
            )

            fake_factory.assert_not_called()
            assert capturer._hwnd == 123
            assert capturer._bg_capture_started is False

    @patch("app.engine.capturer.ImageGrab")
    @patch("app.engine.capturer.win32gui")
    def test_zbl_panic_falls_back_to_image_grab(self, mock_win32gui, mock_grab):
        class PanicException(BaseException):
            pass

        fake_capture = MagicMock()
        fake_capture.try_grab.side_effect = PanicException("wrong thread")
        mock_win32gui.IsWindow.return_value = True
        mock_grab.grab.return_value = MagicMock(spec=Image.Image)

        capturer = Capturer()
        capturer._hwnd = 123
        capturer._bg_capture = fake_capture
        capturer._bg_capture_started = True

        result = capturer.screenshot(1, 2, 3, 4)

        mock_grab.grab.assert_called_once_with(bbox=(1, 2, 4, 6), all_screens=True)
        assert result is mock_grab.grab.return_value
