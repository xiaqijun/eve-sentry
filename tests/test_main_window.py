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


def test_publish_ocr_snapshot_uses_window_context_client_id():
    class FakeClient:
        def __init__(self):
            self.payload = None

        def post_ocr_snapshot(self, **payload):
            self.payload = payload
            return {"created": 1}

    class FakeCombo:
        def currentText(self):
            return "EVE - Current"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._heartbeat_client_id = "detector-client:test"
    window._window_combo = FakeCombo()
    window._intel_system = "S-KSWL"
    window._intel_system_id = 30000142
    window._refresh_intel_location = lambda: True

    MainWindow._publish_ocr_snapshot(
        window,
        ["Alice"],
        context={
            "client_id": "detector-client:test:eve-pilot-a",
            "key": "eve - pilot a",
            "window_title": "EVE - Pilot A",
        },
    )

    assert window._intel_client.payload == {
        "client_id": "detector-client:test:eve-pilot-a",
        "source_instance": "EVE - Pilot A",
        "system_name": "S-KSWL",
        "system_id": 30000142,
        "names": ["Alice"],
    }


def test_publish_intel_uses_window_context_metadata():
    class FakeClient:
        def __init__(self):
            self.payload = None

        def post_observation(self, **payload):
            self.payload = payload
            return {"observation": {"id": "obs_12345678"}}

    class FakeCombo:
        def currentText(self):
            return "EVE - Current"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._window_combo = FakeCombo()
    window._intel_system = "S-KSWL"
    window._intel_system_id = 30000142
    window._intel_system_source = "esi"
    window._refresh_intel_location = lambda: True
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)

    MainWindow._publish_intel(
        window,
        ["Alice"],
        context={
            "client_id": "detector-client:test:eve-pilot-a",
            "key": "eve - pilot a",
            "window_title": "EVE - Pilot A",
        },
    )

    assert window._intel_client.payload["source_instance"] == "EVE - Pilot A"
    assert window._intel_client.payload["metadata"] == {
        "client_id": "detector-client:test:eve-pilot-a",
        "system_source": "esi",
        "target_id": "eve - pilot a",
        "window_title": "EVE - Pilot A",
    }


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

        def discover_files(self):
            return [object()]

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
    assert window._log_messages == [
        "Channel log monitor started: wc.Venal+Br+Te (1 files)"
    ]


def test_channel_monitor_warns_when_no_channel_files_match(monkeypatch):
    class FakeWatcher:
        def __init__(self, log_dir, channels, state_path):
            self.seeded = False

        def discover_files(self):
            return []

        def seed_to_end(self):
            self.seeded = True

    monkeypatch.setattr("app.ui.main_window.ChatLogWatcher", FakeWatcher)
    window = make_channel_window(["wc.Venal+Br+Te"])

    assert MainWindow._start_channel_monitor(window) is True

    assert window._channel_watcher.seeded is True
    assert window._channel_timer.started is True
    assert window._log_messages == [
        "Channel log monitor started with no matching files yet: "
        "wc.Venal+Br+Te. Use full channel names or explicit * / ? wildcards."
    ]


def test_channel_monitor_delegates_parsing_to_server(monkeypatch):
    calls = {}

    def fake_process_once(watcher, api, **kwargs):
        calls["watcher"] = watcher
        calls["api"] = api
        calls["kwargs"] = kwargs
        kwargs["diagnostics"]["last_action"] = "server_parse:1"
        kwargs["diagnostics"]["last_error"] = ""
        kwargs["diagnostics"]["last_success_at"] = "2026-07-07T00:00:00Z"
        return 1

    monkeypatch.setattr("app.ui.main_window.process_once", fake_process_once)
    window = make_channel_window(["wc.Venal+Br+Te"])
    window._channel_watcher = object()
    window._channel_names = ["wc.Venal+Br+Te"]
    window._channel_last_error = ""
    window._channel_last_success_at = ""
    window._publish_heartbeat = lambda: None

    MainWindow._poll_channel_monitor(window)

    assert calls["watcher"] is window._channel_watcher
    assert calls["api"] is window._intel_client
    assert calls["kwargs"]["server_parse"] is True
    assert window._channel_last_action == "server_parse:1"
    assert window._log_messages == ["Channel observations uploaded: 1"]


def test_start_monitor_creates_worker_per_eve_window(monkeypatch):
    created_workers = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def disconnect(self):
            self.callbacks.clear()

    class FakeWorker:
        def __init__(self, capturer, ocr, detector):
            self.capturer = capturer
            self.ocr = ocr
            self.detector = detector
            self.threat_detected = FakeSignal()
            self.ocr_snapshot = FakeSignal()
            self.status_update = FakeSignal()
            self.scan_complete = FakeSignal()
            self.window = None
            self.region = None
            self.interval = None
            self.running = False
            created_workers.append(self)

        def set_window(self, window):
            self.window = dict(window)

        def set_region(self, x, y, w, h):
            self.region = {"x": x, "y": y, "w": w, "h": h}

        def set_interval(self, seconds):
            self.interval = seconds

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def stop(self):
            self.running = False

        def wait(self, timeout):
            return True

    class FakeCapturer:
        def list_eve_windows(self, keyword):
            assert keyword == "EVE -"
            return [
                {"hwnd": 1, "title": "EVE - Pilot A", "x": 0, "y": 0, "w": 800, "h": 600},
                {"hwnd": 2, "title": "EVE - Pilot B", "x": 20, "y": 30, "w": 1000, "h": 800},
            ]

        def get_member_list_region(self, window):
            return {
                "x": window["x"] + window["w"] - 200,
                "y": window["y"],
                "w": 200,
                "h": window["h"],
            }

    class FakeRegionPrefs:
        def resolve_region(self, window):
            if window["title"] == "EVE - Pilot B":
                return {"x": 760, "y": 190, "w": 220, "h": 420}
            return None

    monkeypatch.setattr("app.ui.main_window.MonitorWorker", FakeWorker)
    monkeypatch.setattr("app.ui.main_window.Capturer", lambda: object())
    monkeypatch.setattr("app.ui.main_window.OCREngine", lambda **kwargs: object())
    monkeypatch.setattr("app.ui.main_window.Detector", lambda *args, **kwargs: object())

    window = MainWindow.__new__(MainWindow)
    window._capturer = FakeCapturer()
    window._settings = type(
        "Settings",
        (),
        {
            "get_keyword": lambda self: "EVE -",
            "get_interval": lambda self: 2.0,
            "get_channel_names": lambda self: [],
        },
    )()
    window._region_prefs = FakeRegionPrefs()
    window._whitelist = object()
    window._heartbeat_client_id = "detector-client:test"
    window._workers = {}
    window._worker_contexts = {}
    window._worker = None
    window._refresh_intel_location = lambda force=False: True
    window._start_channel_monitor = lambda: False
    window._publish_heartbeat = lambda: None
    window._refresh_status_cards = lambda: None
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._monitor_btn = type("Button", (), {"setChecked": lambda self, value: None, "setText": lambda self, text: None, "setStyleSheet": lambda self, text: None})()
    window._status_label = type("Label", (), {"setText": lambda self, text: None, "setStyleSheet": lambda self, text: None})()

    MainWindow._start_monitor(window)

    assert len(created_workers) == 2
    assert {worker.window["title"] for worker in created_workers} == {
        "EVE - Pilot A",
        "EVE - Pilot B",
    }
    assert created_workers[0].region == {"x": 600, "y": 0, "w": 200, "h": 600}
    assert created_workers[1].region == {"x": 760, "y": 190, "w": 220, "h": 420}
    assert all(worker.interval == 2.0 for worker in created_workers)
    assert set(window._workers) == {"eve - pilot a", "eve - pilot b"}
    assert {
        context["client_id"] for context in window._worker_contexts.values()
    } == {
        "detector-client:test:eve-pilot-a",
        "detector-client:test:eve-pilot-b",
    }


def test_publish_heartbeat_includes_multi_window_targets():
    class FakeClient:
        def __init__(self):
            self.payload = None

        def post_heartbeat(self, **payload):
            self.payload = payload
            return {"client_id": payload["client_id"], "online": True}

    class FakeWorker:
        def isRunning(self):
            return True

    class FakeCombo:
        def currentText(self):
            return "EVE - Pilot A"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._workers = {"eve - pilot a": FakeWorker(), "eve - pilot b": FakeWorker()}
    window._worker = None
    window._worker_contexts = {
        "eve - pilot a": {
            "key": "eve - pilot a",
            "client_id": "detector-client:test:eve-pilot-a",
            "window_title": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
        },
        "eve - pilot b": {
            "key": "eve - pilot b",
            "client_id": "detector-client:test:eve-pilot-b",
            "window_title": "EVE - Pilot B",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
        },
    }
    window._heartbeat_client_id = "detector-client:test"
    window._heartbeat_interval = 15.0
    window._heartbeat_runtime = {
        "client_version": "test-version",
        "host": "test-host",
    }
    window._heartbeat_last_action = "monitor_started:2"
    window._heartbeat_last_error = ""
    window._heartbeat_last_success_at = "2026-07-07T00:00:00Z"
    window._intel_system = "S-KSWL"
    window._intel_system_source = "esi"
    window._popup_alerts_enabled = False
    window._window_combo = FakeCombo()
    window._channel_watcher = None
    window._channel_names = []
    window._channel_last_action = ""
    window._channel_last_error = ""
    window._channel_last_success_at = ""
    window._last_heartbeat_error = ""
    window._refresh_status_cards = lambda: None

    MainWindow._publish_heartbeat(window)

    details = window._intel_client.payload["details"]
    assert window._intel_client.payload["status"] == "running"
    assert details["target_count"] == 2
    assert details["targets"] == [
        {
            "client_id": "detector-client:test:eve-pilot-a",
            "window_title": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
            "monitoring": True,
        },
        {
            "client_id": "detector-client:test:eve-pilot-b",
            "window_title": "EVE - Pilot B",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
            "monitoring": True,
        },
    ]


def test_stop_monitor_workers_stops_all_workers_and_clears_context():
    class FakeSignal:
        def __init__(self):
            self.disconnects = 0

        def disconnect(self):
            self.disconnects += 1

    class FakeWorker:
        def __init__(self):
            self.threat_detected = FakeSignal()
            self.ocr_snapshot = FakeSignal()
            self.status_update = FakeSignal()
            self.scan_complete = FakeSignal()
            self.running = True
            self.stop_calls = 0
            self.waits = []

        def isRunning(self):
            return self.running

        def stop(self):
            self.stop_calls += 1

        def wait(self, timeout):
            self.waits.append(timeout)
            self.running = False
            return True

    first = FakeWorker()
    second = FakeWorker()
    window = MainWindow.__new__(MainWindow)
    window._workers = {"first": first, "second": second}
    window._worker = first
    window._worker_contexts = {"first": {"window_title": "A"}}
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)

    assert MainWindow._stop_monitor_workers(window, timeout_ms=1234) is True

    assert first.stop_calls == 1
    assert second.stop_calls == 1
    assert first.waits == [1234]
    assert second.waits == [1234]
    assert first.threat_detected.disconnects == 1
    assert first.ocr_snapshot.disconnects == 1
    assert first.status_update.disconnects == 1
    assert first.scan_complete.disconnects == 1
    assert second.threat_detected.disconnects == 1
    assert window._workers == {}
    assert window._worker_contexts == {}
    assert window._worker is None


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
