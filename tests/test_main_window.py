from app.ui.main_window import MainWindow


def test_detector_client_reports_only_when_threats_are_detected():
    window = MainWindow.__new__(MainWindow)
    published = []
    local_alerts = []

    window._publish_intel = lambda threats: published.append(list(threats))
    window._on_threat = lambda threats: local_alerts.append(list(threats))

    MainWindow._on_threat_detected(window, ["Varg Vikernes"])

    assert published == [["Varg Vikernes"]]
    assert local_alerts == []


def test_publish_ocr_snapshot_posts_only_detected_names():
    class FakeClient:
        def __init__(self):
            self.payload = None

        def post_ocr_snapshot(self, **payload):
            self.payload = payload
            return {"created": 1}

    class FakeCombo:
        def currentText(self):
            return "EVE - Hajimi6"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._heartbeat_client_id = "detector-client:test"
    window._window_combo = FakeCombo()
    window._intel_system = "S-KSWL"
    window._intel_system_id = 30000142
    window._refresh_intel_location = lambda: True

    MainWindow._publish_ocr_snapshot(window, ["Alice"])

    assert window._intel_client.payload == {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "system_id": 30000142,
        "names": ["Alice"],
    }
    assert "raw_text" not in window._intel_client.payload
    assert "active" not in window._intel_client.payload
    assert "inactive" not in window._intel_client.payload


class FakeChannelTimer:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.interval = None

    def setInterval(self, value):
        self.interval = value

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeChannelSettings:
    def __init__(self, channels, log_dir="C:/EVE/Chatlogs"):
        self._channels = channels
        self._log_dir = log_dir

    def get_channel_names(self):
        return list(self._channels)

    def get_channel_log_dir(self):
        return self._log_dir


def make_channel_window(channels):
    window = MainWindow.__new__(MainWindow)
    window._settings = FakeChannelSettings(channels)
    window._intel_client = object()
    window._channel_timer = FakeChannelTimer()
    window._channel_watcher = None
    window._channel_state_path = "channel_offsets.json"
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    return window


def test_channel_monitor_does_not_start_without_selected_channel():
    window = make_channel_window([])

    assert MainWindow._start_channel_monitor(window) is False
    assert window._channel_watcher is None
    assert window._channel_timer.started is False


def test_channel_monitor_starts_only_for_selected_channels(monkeypatch):
    created = {}

    class FakeWatcher:
        def __init__(self, log_dir, channels, state_path):
            created["log_dir"] = log_dir
            created["channels"] = channels
            created["state_path"] = state_path
            self.seeded = False

        def seed_to_end(self):
            self.seeded = True

    monkeypatch.setattr("app.ui.main_window.ChatLogWatcher", FakeWatcher)
    window = make_channel_window(["wc.Venal+Br+Te"])

    assert MainWindow._start_channel_monitor(window) is True

    assert created == {
        "log_dir": "C:/EVE/Chatlogs",
        "channels": ["wc.Venal+Br+Te"],
        "state_path": "channel_offsets.json",
    }
    assert window._channel_watcher.seeded is True
    assert window._channel_timer.interval == 5000
    assert window._channel_timer.started is True


def test_switching_selected_window_clears_stale_manual_region():
    class FakeCombo:
        def currentData(self):
            return 99

        def currentText(self):
            return "EVE - Selected"

    class FakeCapturer:
        def __init__(self):
            self.selected = None

        def get_window_info(self, hwnd):
            assert hwnd == 99
            return {
                "hwnd": 99,
                "title": "EVE - Selected",
                "x": 100,
                "y": 200,
                "w": 800,
                "h": 600,
            }

        def select_window(self, *args, **kwargs):
            self.selected = (args, kwargs)

        def get_member_list_region(self, info):
            return {"x": 700, "y": 204, "w": 196, "h": 592}

    class FakeRegionPrefs:
        def resolve_region(self, info):
            return {"x": 650, "y": 220, "w": 200, "h": 300}

    class FakeLabel:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    window = MainWindow.__new__(MainWindow)
    window._window_combo = FakeCombo()
    window._capturer = FakeCapturer()
    window._settings = type("Settings", (), {"get_keyword": lambda self: "EVE -"})()
    window._region_prefs = FakeRegionPrefs()
    window._window_label = FakeLabel()
    window._manual_region = {"x": 1, "y": 2, "w": 3, "h": 4}

    MainWindow._on_window_selected(window, 0)

    assert window._manual_region is None
    assert window._detected_region == {"x": 650, "y": 220, "w": 200, "h": 300}


def test_select_region_passes_window_title_to_selector(monkeypatch):
    created = {}

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeSelector:
        def __init__(self, x, y, w, h, title=""):
            created["args"] = (x, y, w, h)
            created["title"] = title
            self.region_selected = FakeSignal()
            self.selector_closed = FakeSignal()
            self.shown = False

        def show(self):
            self.shown = True

    class FakeCombo:
        def currentData(self):
            return 99

    class FakeCapturer:
        def __init__(self):
            self.activated = []
            self.selected = None

        def activate_window(self, hwnd):
            self.activated.append(hwnd)

        def get_window_info(self, hwnd):
            assert hwnd == 99
            return {
                "hwnd": 99,
                "title": "EVE - Hajimi6",
                "x": 100,
                "y": 200,
                "w": 800,
                "h": 600,
            }

        def select_window(self, *args, **kwargs):
            self.selected = (args, kwargs)

    monkeypatch.setattr("app.ui.main_window.RegionSelector", FakeSelector)
    window = MainWindow.__new__(MainWindow)
    window._window_combo = FakeCombo()
    window._capturer = FakeCapturer()
    window._settings = type("Settings", (), {"get_keyword": lambda self: "EVE -"})()
    window._current_window_info = lambda: {
        "hwnd": 99,
        "title": "EVE - Hajimi6",
        "x": 90,
        "y": 190,
        "w": 700,
        "h": 500,
    }
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window.hide = lambda: None
    window._on_region_selected = lambda *args: None
    window._on_selector_closed = lambda *args: None

    MainWindow._select_region(window)

    assert created == {"args": (100, 200, 800, 600), "title": "EVE - Hajimi6"}
    assert window._selector.shown is True
