import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from app.ui.main_window import (
    CHANNEL_ERROR_BACKOFF_MS,
    CHANNEL_POLL_INTERVAL_MS,
    MainWindow,
)
from app.ui.settings import SettingsPanel

_QT_APP = None

def qt_app():
    global _QT_APP
    _QT_APP = QApplication.instance() or _QT_APP or QApplication([])
    return _QT_APP


def test_detector_client_does_not_post_observation_for_local_threats():
    window = MainWindow.__new__(MainWindow)
    logs = []

    class FakeCombo:
        def currentText(self):
            return "EVE - Current"

    window._window_combo = FakeCombo()
    window._heartbeat_last_action = ""
    window._heartbeat_last_error = ""
    window._log_message = lambda message: logs.append(message)
    window._refresh_status_cards = lambda: None

    MainWindow._on_threat_detected(window, ["Varg Vikernes"])

    assert window._heartbeat_last_action == "local_detection:1"
    assert logs == ["EVE - Current: 本地识别到 1 个名单，已通过 OCR snapshot 上报"]


def test_detector_client_does_not_post_context_observation_for_local_threats():
    window = MainWindow.__new__(MainWindow)
    logs = []

    window._heartbeat_last_action = ""
    window._heartbeat_last_error = ""
    window._log_message = lambda message: logs.append(message)
    window._refresh_status_cards = lambda: None

    MainWindow._on_threat_detected(
        window,
        ["Alice", "Bob"],
        context={"window_title": "EVE - Pilot A"},
    )

    assert window._heartbeat_last_action == "local_detection:2"
    assert logs == ["EVE - Pilot A: 本地识别到 2 个名单，已通过 OCR snapshot 上报"]


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
            "source_instance": "EVE - Pilot A #1 · hwnd 1 · 800x600",
            "window_title": "EVE - Pilot A",
        },
    )

    assert window._intel_client.payload == {
        "client_id": "detector-client:test:eve-pilot-a",
        "source_instance": "EVE - Pilot A #1 · hwnd 1 · 800x600",
        "system_name": "S-KSWL",
        "system_id": 30000142,
        "names": ["Alice"],
    }


class FakeChannelTimer:
    def __init__(self):
        self.started = False
        self.stopped = False
        self._interval = None

    def setInterval(self, value):
        self._interval = value

    def interval(self):
        return self._interval

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


def test_settings_panel_loads_and_saves_channel_config(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_CHANNEL", raising=False)
    monkeypatch.delenv("EVE_SENTRY_CHATLOG_DIR", raising=False)
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "channels": "wc.Venal+Br+Te, *Intel",
                "chatlog_dir": "C:/EVE/Chatlogs",
            }
        ),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel.get_channel_names() == ["wc.Venal+Br+Te", "*Intel"]
    assert panel.get_channel_log_dir() == "C:/EVE/Chatlogs"

    panel._channel_enabled.setChecked(False)
    panel._channel_edit.setText("Alliance Intel")
    panel._channel_log_dir_edit.setText("D:/Logs/Chatlogs")
    panel.save_channel_config()

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == {
        "enabled": False,
        "channels": "Alliance Intel",
        "chatlog_dir": "D:/Logs/Chatlogs",
        "recent_days": 30,
    }
    assert panel.get_channel_names() == []


def test_settings_panel_environment_overrides_saved_channel_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EVE_SENTRY_CHANNEL", "Env Intel")
    monkeypatch.setenv("EVE_SENTRY_CHATLOG_DIR", "E:/Env/Chatlogs")
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": False,
                "channels": "Saved Intel",
                "chatlog_dir": "C:/Saved/Chatlogs",
            }
        ),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel.get_channel_names() == ["Env Intel"]
    assert panel.get_channel_log_dir() == "E:/Env/Chatlogs"


def test_settings_panel_discovers_channel_list_from_chatlogs(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_CHANNEL", raising=False)
    monkeypatch.delenv("EVE_SENTRY_CHATLOG_DIR", raising=False)
    chatlogs = tmp_path / "Chatlogs"
    chatlogs.mkdir()
    (chatlogs / "Alliance Intel_20260630_120000.txt").write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )
    (chatlogs / "wc.Venal+Br+Te_20260702_121156_2124219939.txt").write_text(
        "[ 2026.07.02 12:11:56 ] Scout B > S-KSWL clear\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "channels": "Alliance Intel",
                "chatlog_dir": str(chatlogs),
            }
        ),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert [
        panel._channel_list.item(index).text()
        for index in range(panel._channel_list.count())
    ] == ["Alliance Intel", "wc.Venal+Br+Te"]
    assert panel.get_channel_names() == ["Alliance Intel"]

    panel._channel_list.item(1).setCheckState(Qt.CheckState.Checked)
    panel.save_channel_config()

    assert panel.get_channel_names() == ["Alliance Intel", "wc.Venal+Br+Te"]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["channels"] == "Alliance Intel, wc.Venal+Br+Te"


def test_settings_panel_filters_historical_channel_files(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_CHANNEL", raising=False)
    monkeypatch.delenv("EVE_SENTRY_CHATLOG_DIR", raising=False)
    chatlogs = tmp_path / "Chatlogs"
    chatlogs.mkdir()
    recent = chatlogs / "Current Intel_20260708_120000.txt"
    old = chatlogs / "Old Intel_20240101_120000.txt"
    recent.write_text("[ 2026.07.08 12:00:00 ] Scout > active\n", encoding="utf-8")
    old.write_text("[ 2024.01.01 12:00:00 ] Scout > old\n", encoding="utf-8")
    old_time = recent.stat().st_mtime - (60 * 24 * 60 * 60)
    os.utime(old, (old_time, old_time))
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "channels": "",
                "chatlog_dir": str(chatlogs),
                "recent_days": 30,
            }
        ),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert [
        panel._channel_list.item(index).text()
        for index in range(panel._channel_list.count())
    ] == ["Current Intel"]

    panel._channel_recent_days_spin.setValue(0)

    assert [
        panel._channel_list.item(index).text()
        for index in range(panel._channel_list.count())
    ] == ["Current Intel", "Old Intel"]


def make_channel_window(channels):
    window = MainWindow.__new__(MainWindow)
    window._settings = FakeChannelSettings(channels)
    window._intel_client = object()
    window._channel_timer = FakeChannelTimer()
    window._channel_watcher = None
    window._channel_state_path = "channel_offsets.json"
    window._channel_error_backoff_ms = CHANNEL_ERROR_BACKOFF_MS
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    return window


def test_channel_monitor_saves_config_before_start(monkeypatch):
    class SaveableSettings(FakeChannelSettings):
        def __init__(self):
            super().__init__(["wc.Venal+Br+Te"])
            self.saved = 0

        def save_channel_config(self):
            self.saved += 1

    class FakeWatcher:
        def __init__(self, log_dir, channels, state_path):
            pass

        def discover_files(self):
            return [object()]

        def seed_to_end(self):
            pass

    monkeypatch.setattr("app.ui.main_window.ChatLogWatcher", FakeWatcher)
    window = make_channel_window([])
    window._settings = SaveableSettings()

    assert MainWindow._start_channel_monitor(window) is True
    assert window._settings.saved == 1


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
    assert window._channel_timer.interval() == CHANNEL_POLL_INTERVAL_MS
    assert window._channel_timer.started is True
    assert window._log_messages == [
        "频道日志监控已启动：wc.Venal+Br+Te（匹配 1 个日志文件）"
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
        "频道日志监控已启动，但暂未匹配到日志文件："
        "wc.Venal+Br+Te。请使用完整频道名，或显式使用 * / ? 通配符。"
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
    assert window._channel_timer.interval() == CHANNEL_POLL_INTERVAL_MS
    assert window._log_messages == ["Channel observations uploaded: 1"]


def test_channel_monitor_backs_off_after_upload_error(monkeypatch):
    def fake_process_once(_watcher, _api, **_kwargs):
        raise RuntimeError("timed out")

    monkeypatch.setattr("app.ui.main_window.process_once", fake_process_once)
    window = make_channel_window(["wc.Venal+Br+Te"])
    window._channel_watcher = object()
    window._channel_names = ["wc.Venal+Br+Te"]
    window._channel_last_error = ""
    window._channel_last_success_at = ""
    heartbeat_calls = []
    window._publish_heartbeat = lambda: heartbeat_calls.append(True)

    MainWindow._poll_channel_monitor(window)

    assert window._channel_last_action == "observation_error"
    assert window._channel_last_error == "timed out"
    assert window._channel_timer.interval() == CHANNEL_ERROR_BACKOFF_MS
    assert window._log_messages == ["Channel log upload failed: timed out"]
    assert heartbeat_calls == [True]


def test_channel_monitor_restores_poll_interval_after_success(monkeypatch):
    def fake_process_once(_watcher, _api, **kwargs):
        kwargs["diagnostics"]["last_action"] = "server_parse_idle"
        kwargs["diagnostics"]["last_error"] = ""
        return 0

    monkeypatch.setattr("app.ui.main_window.process_once", fake_process_once)
    window = make_channel_window(["wc.Venal+Br+Te"])
    window._channel_watcher = object()
    window._channel_names = ["wc.Venal+Br+Te"]
    window._channel_last_error = "timed out"
    window._channel_last_success_at = ""
    window._channel_timer.setInterval(CHANNEL_ERROR_BACKOFF_MS)
    window._publish_heartbeat = lambda: None

    MainWindow._poll_channel_monitor(window)

    assert window._channel_last_action == "server_parse_idle"
    assert window._channel_last_error == ""
    assert window._channel_timer.interval() == CHANNEL_POLL_INTERVAL_MS


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
    assert set(window._workers) == {
        "hwnd:1:eve - pilot a",
        "hwnd:2:eve - pilot b",
    }
    assert {
        context["client_id"] for context in window._worker_contexts.values()
    } == {
        "detector-client:test:hwnd-1-eve-pilot-a",
        "detector-client:test:hwnd-2-eve-pilot-b",
    }
    assert {
        context["source_instance"] for context in window._worker_contexts.values()
    } == {
        "EVE - Pilot A",
        "EVE - Pilot B",
    }


def test_build_monitor_targets_keeps_duplicate_window_titles_distinct():
    class FakeCapturer:
        def list_eve_windows(self, keyword):
            assert keyword == "EVE -"
            return [
                {"hwnd": 1, "title": "EVE - Pilot", "x": 0, "y": 0, "w": 800, "h": 600},
                {"hwnd": 2, "title": "EVE - Pilot", "x": 20, "y": 30, "w": 1000, "h": 800},
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
            return None

    window = MainWindow.__new__(MainWindow)
    window._capturer = FakeCapturer()
    window._settings = type(
        "Settings",
        (),
        {
            "get_keyword": lambda self: "EVE -",
        },
    )()
    window._region_prefs = FakeRegionPrefs()
    window._heartbeat_client_id = "detector-client:test"

    targets = MainWindow._build_monitor_targets(window)

    assert [target["key"] for target in targets] == [
        "hwnd:1:eve - pilot",
        "hwnd:2:eve - pilot",
    ]
    assert [target["client_id"] for target in targets] == [
        "detector-client:test:hwnd-1-eve-pilot",
        "detector-client:test:hwnd-2-eve-pilot",
    ]
    assert [target["source_instance"] for target in targets] == [
        "EVE - Pilot #1 · hwnd 1 · 800x600",
        "EVE - Pilot #2 · hwnd 2 · 1000x800",
    ]


def test_detect_window_labels_duplicate_titles_with_hwnd_and_size():
    class FakeCombo:
        def __init__(self):
            self.items = []
            self.current_index = -1

        def blockSignals(self, value):
            self.blocked = value

        def clear(self):
            self.items.clear()

        def addItem(self, label, data):
            self.items.append((label, data))

        def setCurrentIndex(self, index):
            self.current_index = index

        def currentData(self):
            if self.current_index < 0:
                return None
            return self.items[self.current_index][1]

        def currentText(self):
            if self.current_index < 0:
                return ""
            return self.items[self.current_index][0]

    class FakeCapturer:
        def list_eve_windows(self, keyword):
            assert keyword == "EVE -"
            return [
                {"hwnd": 1, "title": "EVE - Pilot", "x": 0, "y": 0, "w": 800, "h": 600},
                {"hwnd": 2, "title": "EVE - Pilot", "x": 20, "y": 30, "w": 1000, "h": 800},
            ]

        def get_window_info(self, hwnd):
            return next(
                window
                for window in self.list_eve_windows("EVE -")
                if window["hwnd"] == hwnd
            )

        def select_window(self, *args, **kwargs):
            self.selected = (args, kwargs)

        def get_member_list_region(self, window):
            return {"x": window["x"] + window["w"] - 200, "y": window["y"], "w": 200, "h": window["h"]}

    class FakeRegionPrefs:
        def resolve_region(self, window):
            return None

    class FakeLabel:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    window = MainWindow.__new__(MainWindow)
    window._settings = type("Settings", (), {"get_keyword": lambda self: "EVE -"})()
    window._capturer = FakeCapturer()
    window._window_combo = FakeCombo()
    window._window_label = FakeLabel()
    window._region_prefs = FakeRegionPrefs()
    window._refresh_status_cards = lambda: None
    window._refresh_window_status_table = lambda: None
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)

    MainWindow._detect_window(window)

    assert window._window_combo.items == [
        ("EVE - Pilot #1 · hwnd 1 · 800x600", 1),
        ("EVE - Pilot #2 · hwnd 2 · 1000x800", 2),
    ]
    assert window._window_label.text == (
        "窗口：EVE - Pilot #1 · hwnd 1 · 800x600 -> 成员列表 200x600"
    )


class FakeStatusTable:
    def __init__(self, columns=4):
        self._columns = columns
        self.rows = []
        self.resized = False

    def setRowCount(self, count):
        self.rows = [[None for _ in range(self._columns)] for _ in range(count)]

    def setItem(self, row, column, item):
        self.rows[row][column] = item

    def columnCount(self):
        return self._columns

    def resizeColumnsToContents(self):
        self.resized = True


def table_text(table):
    return [
        [cell.text() if cell is not None else "" for cell in row]
        for row in table.rows
    ]


def test_window_status_table_lists_worker_contexts():
    table = FakeStatusTable()
    window = MainWindow.__new__(MainWindow)
    window._window_status_table = table
    window._worker_contexts = {
        "first": {
            "window_title": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
            "runtime_status": "运行中",
            "last_action": "监控线程已启动",
        },
        "second": {
            "window_title": "EVE - Pilot B",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
            "runtime_status": "扫描中",
            "last_action": "OCR 名单 2",
        },
    }

    MainWindow._refresh_window_status_table(window)

    assert table_text(table) == [
        ["EVE - Pilot A", "200x600 @ 600,0", "运行中", "监控线程已启动"],
        ["EVE - Pilot B", "220x420 @ 760,190", "扫描中", "OCR 名单 2"],
    ]
    assert table.resized is True


def test_update_window_status_records_last_action():
    table = FakeStatusTable()
    context = {
        "window_title": "EVE - Pilot A",
        "region": {"x": 600, "y": 0, "w": 200, "h": 600},
    }
    window = MainWindow.__new__(MainWindow)
    window._window_status_table = table
    window._worker_contexts = {"first": context}

    MainWindow._update_window_status(window, context, "识别到名单", "本地名单 3")

    assert context["runtime_status"] == "识别到名单"
    assert context["last_action"] == "本地名单 3"
    assert context["updated_at"]
    assert table_text(table) == [
        ["EVE - Pilot A", "200x600 @ 600,0", "识别到名单", "本地名单 3"]
    ]


def test_start_monitor_allows_channel_only_without_eve_windows():
    class FakeButton:
        def __init__(self):
            self.text = ""
            self.style = ""
            self.checked = True

        def setChecked(self, value):
            self.checked = value

        def setText(self, text):
            self.text = text

        def setStyleSheet(self, text):
            self.style = text

    class FakeLabel:
        def __init__(self):
            self.text = ""
            self.style = ""

        def setText(self, text):
            self.text = text

        def setStyleSheet(self, text):
            self.style = text

    window = MainWindow.__new__(MainWindow)
    window._build_monitor_targets = lambda: []
    window._detect_window = lambda: None
    window._start_channel_monitor = lambda: True
    window._publish_heartbeat_called = 0
    window._publish_heartbeat = lambda: setattr(
        window,
        "_publish_heartbeat_called",
        window._publish_heartbeat_called + 1,
    )
    window._refresh_status_cards_called = 0
    window._refresh_status_cards = lambda: setattr(
        window,
        "_refresh_status_cards_called",
        window._refresh_status_cards_called + 1,
    )
    window._refresh_intel_location = lambda force=False: True
    window._monitor_btn = FakeButton()
    window._status_label = FakeLabel()
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._heartbeat_last_action = ""
    window._heartbeat_last_error = "previous"
    window._heartbeat_last_success_at = ""

    MainWindow._start_monitor(window)

    assert window._monitor_btn.text == "停止监控"
    assert window._status_label.text == "频道日志监控中"
    assert window._log_messages == ["未发现 EVE 窗口，已仅启动频道日志监控"]
    assert window._heartbeat_last_action == "channel_monitor_started"
    assert window._heartbeat_last_error == ""
    assert window._heartbeat_last_success_at
    assert window._publish_heartbeat_called == 1
    assert window._refresh_status_cards_called == 1


def test_auto_start_monitor_checks_button_and_starts_once():
    class FakeButton:
        def __init__(self):
            self.checked = False
            self.set_checked_calls = []

        def isChecked(self):
            return self.checked

        def setChecked(self, value):
            self.checked = value
            self.set_checked_calls.append(value)

    window = MainWindow.__new__(MainWindow)
    window._monitor_btn = FakeButton()
    started = []
    window._start_monitor = lambda: started.append(True)

    MainWindow._auto_start_monitor(window)
    MainWindow._auto_start_monitor(window)

    assert window._monitor_btn.set_checked_calls == [True]
    assert started == [True]


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
            "source_instance": "EVE - Pilot A",
            "window_title": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
        },
        "eve - pilot b": {
            "key": "eve - pilot b",
            "client_id": "detector-client:test:eve-pilot-b",
            "source_instance": "EVE - Pilot B",
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
            "source_instance": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
            "monitoring": True,
        },
        {
            "client_id": "detector-client:test:eve-pilot-b",
            "window_title": "EVE - Pilot B",
            "source_instance": "EVE - Pilot B",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
            "monitoring": True,
        },
    ]


def test_publish_heartbeat_marks_channel_only_monitor_as_running():
    class FakeClient:
        def __init__(self):
            self.payload = None

        def post_heartbeat(self, **payload):
            self.payload = payload
            return {"client_id": payload["client_id"], "online": True}

    class FakeCombo:
        def currentText(self):
            return ""

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._workers = {}
    window._worker = None
    window._worker_contexts = {}
    window._heartbeat_client_id = "detector-client:test"
    window._heartbeat_interval = 15.0
    window._heartbeat_runtime = {
        "client_version": "test-version",
        "host": "test-host",
    }
    window._heartbeat_last_action = "channel_monitor_started"
    window._heartbeat_last_error = ""
    window._heartbeat_last_success_at = "2026-07-07T00:00:00Z"
    window._intel_system = "S-KSWL"
    window._intel_system_source = "env"
    window._popup_alerts_enabled = False
    window._window_combo = FakeCombo()
    window._channel_watcher = object()
    window._channel_names = ["wc.Venal+Br+Te"]
    window._channel_last_action = "server_parse_idle"
    window._channel_last_error = ""
    window._channel_last_success_at = ""
    window._last_heartbeat_error = ""
    window._refresh_status_cards = lambda: None

    MainWindow._publish_heartbeat(window)

    assert window._intel_client.payload["status"] == "running"
    details = window._intel_client.payload["details"]
    assert details["mode"] == "channel_monitoring"
    assert details["monitoring"] is False
    assert details["channel_monitoring"] is True
    assert details["channels"] == ["wc.Venal+Br+Te"]
    assert details["channel_last_action"] == "server_parse_idle"


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
