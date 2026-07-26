import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionSpinBox

from app.ui.main_window import MainWindow
from app.ui.settings import SettingsPanel
from app.ui.settings import DEFAULT_INTEL_URL
from app.ui.theme import APP_QSS

_QT_APP = None

def qt_app():
    global _QT_APP
    _QT_APP = QApplication.instance() or _QT_APP or QApplication([])
    return _QT_APP


def test_close_event_runs_full_shutdown_instead_of_hiding_to_tray():
    calls = []

    class FakeEvent:
        def accept(self):
            calls.append("accepted")

    class FakeWindow:
        def _quit_app(self):
            calls.append("shutdown")

    MainWindow.closeEvent(FakeWindow(), FakeEvent())

    assert calls == ["shutdown", "accepted"]


def test_quit_app_waits_for_ocr_workers_before_exiting(monkeypatch):
    calls = []

    class FakeNetworkTasks:
        def shutdown(self):
            calls.append("network")

    class FakeTray:
        def hide(self):
            calls.append("tray")

    class FakeWindow:
        def __init__(self):
            self._network_tasks = FakeNetworkTasks()
            self._tray = FakeTray()

        def _stop_alert(self, *, wait_for_worker=False):
            calls.append(("alert", wait_for_worker))

        def _stop_monitor(self, *, wait_for_workers=False):
            calls.append(("monitor", wait_for_workers))

    monkeypatch.setattr(QApplication, "quit", lambda: calls.append("quit"))

    MainWindow._quit_app(FakeWindow())

    assert calls == [
        ("alert", True),
        ("monitor", True),
        "network",
        "tray",
        "quit",
    ]


def test_alert_toggle_starts_and_stops_embedded_controller(monkeypatch):
    calls = []
    qt_app()

    class FakeButton:
        def setChecked(self, value):
            calls.append(("checked", value))

        def setText(self, value):
            calls.append(("text", value))

        def setStyleSheet(self, value):
            calls.append(("style", bool(value)))

    class FakeController:
        def __init__(self, app, args, **kwargs):
            calls.append(
                (
                    "init",
                    args.server,
                    kwargs["tray_enabled"],
                    kwargs["notification_callback"],
                )
            )

        def start(self):
            calls.append("start")

        def show_monitoring_systems(self, systems):
            calls.append(("systems", list(systems)))

        def stop(self, *, wait_for_worker=True):
            calls.append(("stop", wait_for_worker))

        def is_running(self):
            return False

    window = MainWindow.__new__(MainWindow)
    window._intel_url = "http://intel.example"
    window._alert_controller = None
    window._workers = {"eve-hajimi6": object()}
    window._worker_contexts = {
        "eve-hajimi6": {"system_name": "S-KSWL"},
    }
    window._alert_btn = FakeButton()
    window._log_message = lambda message: calls.append(("log", message))

    monkeypatch.setattr("app.ui.main_window.AlertTrayController", FakeController)
    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    MainWindow._start_alert(window)

    assert calls[:3] == [
        ("init", "http://intel.example", False, None),
        ("systems", ["S-KSWL"]),
        "start",
    ]
    assert window._alert_controller is not None
    assert ("text", "关闭预警") in calls

    MainWindow._stop_alert(window)

    assert ("stop", False) in calls
    assert window._alert_controller is None
    assert window._stopping_alert_controllers == set()
    assert ("text", "开启预警") in calls


def test_detector_client_has_no_local_threat_handler():
    assert not hasattr(MainWindow, "_on_threat_detected")


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
    window._refresh_intel_location = lambda force=False, context=None: True

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

    MainWindow._publish_ocr_snapshot(
        window,
        ["Alice"],
        hostile_icon_count=1,
    )

    assert window._intel_client.payload["hostile_icon_count"] == 1


def test_hostile_icon_detection_notifies_immediately():
    class FakeAlertController:
        def __init__(self):
            self.counts = []

        def update_local_hostile_count(self, system_name, count):
            self.counts.append((system_name, count))

    window = MainWindow.__new__(MainWindow)
    window._alert_controller = FakeAlertController()
    window._messages = []
    window._updates = []
    window._log_message = window._messages.append
    window._update_window_status = lambda *args: window._updates.append(args)
    context = {"window_title": "EVE - Hajimi6", "system_name": "S-KSWL"}

    MainWindow._on_hostile_icon_detected(window, 2, context)

    assert window._alert_controller.counts == [("S-KSWL", 2)]
    assert window._updates == [
        (context, "敌对告警", "❗ S-KSWL 来敌")
    ]

    MainWindow._on_hostile_icon_detected(window, 0, context)

    assert window._alert_controller.counts[-1] == ("S-KSWL", 0)
    assert window._updates[-1] == (context, "监控中", "✅ S-KSWL 清空")


def test_hostile_icon_detection_is_silent_when_alerts_are_disabled():
    class FakeTray:
        def __init__(self):
            self.messages = []

        def showMessage(self, title, message):
            self.messages.append((title, message))

    window = MainWindow.__new__(MainWindow)
    window._alert_controller = None
    window._tray = FakeTray()
    window._messages = []
    window._updates = []
    window._log_message = window._messages.append
    window._update_window_status = lambda *args: window._updates.append(args)
    MainWindow._on_hostile_icon_detected(
        window,
        2,
        {"window_title": "EVE - Hajimi6", "system_name": "S-KSWL"},
    )

    assert window._tray.messages == []
    assert window._messages == []
    assert window._updates == []


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
    window._refresh_intel_location = lambda force=False, context=None: True

    MainWindow._publish_ocr_snapshot(
        window,
        ["Alice"],
        context={
            "client_id": "detector-client:test:eve-pilot-a",
            "key": "eve - pilot a",
            "source_instance": "EVE - Pilot A #1 · hwnd 1 · 800x600",
            "window_title": "EVE - Pilot A",
            "character_name": "Pilot A",
            "system_name": "HB-FSO",
            "system_id": 30000242,
            "system_source": "chatlog",
        },
    )

    assert window._intel_client.payload == {
        "client_id": "detector-client:test:eve-pilot-a",
        "source_instance": "EVE - Pilot A #1 · hwnd 1 · 800x600",
        "system_name": "HB-FSO",
        "system_id": 30000242,
        "names": ["Alice"],
    }


def test_publish_ocr_snapshot_uses_each_window_system_context():
    class FakeClient:
        def __init__(self):
            self.payloads = []

        def post_ocr_snapshot(self, **payload):
            self.payloads.append(payload)
            return {"created": 1}

    class FakeCombo:
        def currentText(self):
            return "EVE - Hajimi6"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._heartbeat_client_id = "detector-client:test"
    window._window_combo = FakeCombo()
    window._refresh_intel_location = lambda force=False, context=None: True
    contexts = [
        {
            "client_id": "detector-client:test:hajimi6",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "system_id": None,
        },
        {
            "client_id": "detector-client:test:hajimi5",
            "source_instance": "EVE - Hajimi5",
            "system_name": "HB-FSO",
            "system_id": None,
        },
    ]

    MainWindow._publish_ocr_snapshot(window, ["Alice"], context=contexts[0])
    MainWindow._publish_ocr_snapshot(window, ["Bob"], context=contexts[1])

    assert [payload["system_name"] for payload in window._intel_client.payloads] == [
        "S-KSWL",
        "HB-FSO",
    ]


def test_publish_ocr_snapshot_dispatches_network_work_off_the_ui_thread():
    class FakeClient:
        def __init__(self):
            self.payload = None

        def post_ocr_snapshot(self, **payload):
            self.payload = payload
            return {"created": 1}

    class FakeRunner:
        def submit_latest(self, key, task, context):
            self.key = key
            self.task = task
            self.context = context
            return True

    class FakeCombo:
        def currentText(self):
            return "EVE - Hajimi6"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FakeClient()
    window._network_tasks = FakeRunner()
    window._heartbeat_client_id = "detector-client:test"
    window._window_combo = FakeCombo()
    window._intel_system = "S-KSWL"
    window._intel_system_id = 30000142
    window._refresh_intel_location = lambda force=False, context=None: True

    MainWindow._publish_ocr_snapshot(window, ["Alice"])

    assert window._intel_client.payload is None
    assert window._network_tasks.key == "ocr:detector-client:test"
    assert window._network_tasks.context["kind"] == "ocr"
    window._network_tasks.task()
    assert window._intel_client.payload["names"] == ["Alice"]


def test_refresh_intel_location_uses_only_local_chatlog(monkeypatch):
    class FakeSettings:
        def get_channel_log_dir(self):
            return "C:/EVE/Chatlogs"

    class FakeClient:
        calls = 0

        def current_esi_system(self):
            self.calls += 1
            raise RuntimeError("monitor client must not call ESI")

    class Detection:
        system_name = "S-KSWL"

    monkeypatch.setattr(
        "app.ui.main_window.find_latest_local_system",
        lambda log_dir, character_name="": (
            Detection()
            if log_dir == "C:/EVE/Chatlogs" and character_name == ""
            else None
        ),
    )
    window = MainWindow.__new__(MainWindow)
    window._use_local_system_log = True
    client = FakeClient()
    window._intel_client = client
    window._settings = FakeSettings()
    window._intel_system = "Unknown"
    window._intel_system_id = None
    window._intel_system_source = "default"
    window._location_next_check = 0.0
    window._location_refresh_ttl = 5.0
    window._last_local_system_error = ""
    window._heartbeat_last_action = ""
    window._heartbeat_last_success_at = ""
    window._heartbeat_last_error = ""
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._refresh_status_cards = lambda: None

    assert MainWindow._refresh_intel_location(window, force=True) is True

    assert window._intel_system == "S-KSWL"
    assert window._intel_system_id is None
    assert window._intel_system_source == "chatlog"
    assert window._heartbeat_last_action == "local_system_sync"
    assert client.calls == 0


def test_settings_panel_removes_channel_alert_controls(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_CHATLOG_DIR", raising=False)
    monkeypatch.delenv("EVE_SENTRY_INTEL_URL", raising=False)
    monkeypatch.setattr(
        "app.ui.settings.resolve_chatlog_dir",
        lambda preferred=None: preferred or DEFAULT_CHATLOG_DIR,
    )
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "channels": "wc.Venal+Br+Te, *Intel",
                "chatlog_dir": "C:/EVE/Chatlogs",
                "recent_days": 30,
                "scan_interval": 2,
                "window_keyword": "EVE -",
            }
        ),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel.get_channel_log_dir() == "C:/EVE/Chatlogs"
    assert not hasattr(panel, "get_channel_names")
    assert not hasattr(panel, "channel_settings_changed")
    assert not hasattr(panel, "_channel_enabled")
    assert not hasattr(panel, "_channel_list")

    panel._interval_spin.setValue(5)
    panel._keyword_edit.setText("EVE - Pilot")
    panel.save_channel_config()

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "chatlog_dir": "C:/EVE/Chatlogs",
        "scan_interval": 5,
        "window_keyword": "EVE - Pilot",
        "server_url": DEFAULT_INTEL_URL,
    }


def test_settings_panel_environment_overrides_saved_chatlog_dir(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("EVE_SENTRY_CHATLOG_DIR", "E:/Env/Chatlogs")
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps({"chatlog_dir": "C:/Saved/Chatlogs"}),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel.get_channel_log_dir() == "E:/Env/Chatlogs"


def test_settings_panel_persists_normalized_server_url(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_INTEL_URL", raising=False)
    config_path = tmp_path / "channel_settings.json"
    qt_app()
    panel = SettingsPanel(config_path=config_path)
    changes = []
    panel.server_url_changed.connect(changes.append)

    panel._server_url_edit.setText("intel.example:8765/")
    panel._server_url_edit.editingFinished.emit()

    assert panel.get_server_url() == "http://intel.example:8765"
    assert changes == ["http://intel.example:8765"]
    assert json.loads(config_path.read_text(encoding="utf-8"))["server_url"] == (
        "http://intel.example:8765"
    )

    reloaded = SettingsPanel(config_path=config_path)
    assert reloaded.get_server_url() == "http://intel.example:8765"


def test_settings_panel_environment_overrides_saved_server_url(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("EVE_SENTRY_INTEL_URL", "https://env.example/")
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps({"server_url": "http://saved.example"}),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel.get_server_url() == "https://env.example"


def test_apply_server_url_rebuilds_intel_client():
    replacement_client = object()
    window = MainWindow.__new__(MainWindow)
    window._intel_url = "http://old.example"
    window._intel_client = object()
    window._alert_controller = None
    window._uploads_enabled = False
    window._create_intel_client = lambda: replacement_client
    window._last_heartbeat_error = "old"
    window._heartbeat_last_error = "old"
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._refresh_status_cards = lambda: None

    MainWindow._apply_server_url(window, "http://new.example/")

    assert window._intel_url == "http://new.example"
    assert window._intel_client is replacement_client
    assert window._last_heartbeat_error == ""
    assert window._heartbeat_last_error == ""


def test_spinbox_buttons_match_visible_right_edge(tmp_path):
    app = qt_app()
    panel = SettingsPanel(config_path=tmp_path / "settings.json")
    panel.setStyleSheet(APP_QSS)
    panel.resize(260, 700)
    panel.show()
    app.processEvents()

    spinbox = panel._interval_spin
    option = QStyleOptionSpinBox()
    spinbox.initStyleOption(option)
    up_rect = spinbox.style().subControlRect(
        QStyle.ComplexControl.CC_SpinBox,
        option,
        QStyle.SubControl.SC_SpinBoxUp,
        spinbox,
    )
    down_rect = spinbox.style().subControlRect(
        QStyle.ComplexControl.CC_SpinBox,
        option,
        QStyle.SubControl.SC_SpinBoxDown,
        spinbox,
    )

    assert up_rect.width() >= 26
    assert down_rect.width() >= 26
    assert up_rect.left() == down_rect.left()
    assert up_rect.right() == spinbox.rect().right()
    assert down_rect.right() == spinbox.rect().right()
    assert up_rect.bottom() < down_rect.bottom()

    panel.close()


def test_settings_panel_persists_and_emits_live_scan_changes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("EVE_SENTRY_SCAN_INTERVAL", raising=False)
    monkeypatch.delenv("EVE_SENTRY_WINDOW_KEYWORD", raising=False)
    config_path = tmp_path / "channel_settings.json"

    qt_app()
    panel = SettingsPanel(config_path=config_path)
    scan_changes = []
    panel.scan_settings_changed.connect(lambda: scan_changes.append(True))

    panel._interval_spin.setValue(5)
    panel._keyword_edit.setText("EVE - Pilot")
    panel._keyword_edit.editingFinished.emit()

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["scan_interval"] == 5
    assert saved["window_keyword"] == "EVE - Pilot"
    assert len(scan_changes) == 2


def test_start_monitor_creates_worker_only_for_selected_eve_window(monkeypatch):
    created_workers = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def disconnect(self):
            self.callbacks.clear()

    class FakeWorker:
        def __init__(self, capturer, ocr):
            self.capturer = capturer
            self.ocr = ocr
            self.ocr_snapshot = FakeSignal()
            self.hostile_detected = FakeSignal()
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

    window = MainWindow.__new__(MainWindow)
    window._capturer = FakeCapturer()
    window._settings = type(
        "Settings",
        (),
        {
            "get_keyword": lambda self: "EVE -",
            "get_interval": lambda self: 2.0,
        },
    )()
    window._region_prefs = FakeRegionPrefs()
    window._heartbeat_client_id = "detector-client:test"
    window._current_window_info = lambda: {
        "hwnd": 2,
        "title": "EVE - Pilot B",
        "x": 20,
        "y": 30,
        "w": 1000,
        "h": 800,
    }
    window._workers = {}
    window._worker_contexts = {}
    window._worker = None
    resolved_characters = []

    def refresh_location(force=False, context=None):
        assert force is True
        resolved_characters.append(context["character_name"])
        context["system_name"] = {
            "Pilot A": "S-KSWL",
            "Pilot B": "HB-FSO",
        }[context["character_name"]]
        context["system_source"] = "chatlog"
        return True

    window._refresh_intel_location = refresh_location
    window._publish_heartbeat = lambda: None
    window._refresh_status_cards = lambda: None
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._monitor_btn = type("Button", (), {"setChecked": lambda self, value: None, "setText": lambda self, text: None, "setStyleSheet": lambda self, text: None})()
    window._status_label = type("Label", (), {"setText": lambda self, text: None, "setStyleSheet": lambda self, text: None})()

    MainWindow._start_monitor(window)

    assert len(created_workers) == 1
    assert created_workers[0].window["title"] == "EVE - Pilot B"
    assert created_workers[0].region == {"x": 760, "y": 190, "w": 220, "h": 420}
    assert all(worker.interval == 2.0 for worker in created_workers)
    assert set(window._workers) == {"hwnd:2:eve - pilot b"}
    assert {
        context["client_id"] for context in window._worker_contexts.values()
    } == {"detector-client:test:hwnd-2-eve-pilot-b"}
    assert {
        context["source_instance"] for context in window._worker_contexts.values()
    } == {"EVE - Pilot B"}
    assert resolved_characters == ["Pilot B"]


def test_build_monitor_targets_uses_only_selected_window():
    class FakeCapturer:
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
    window._current_window_info = lambda: {
        "hwnd": 2,
        "title": "EVE - Pilot B",
        "x": 20,
        "y": 30,
        "w": 1000,
        "h": 800,
    }

    targets = MainWindow._build_monitor_targets(window)

    assert [target["key"] for target in targets] == ["hwnd:2:eve - pilot b"]
    assert [target["client_id"] for target in targets] == [
        "detector-client:test:hwnd-2-eve-pilot-b"
    ]
    assert [target["source_instance"] for target in targets] == ["EVE - Pilot B"]
    assert [target["character_name"] for target in targets] == ["Pilot B"]


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


def test_refresh_detected_windows_adds_new_window_and_keeps_selection():
    class FakeCombo:
        def __init__(self):
            self.items = []
            self.current_index = -1

        def blockSignals(self, value):
            self.blocked = value

        def clear(self):
            self.items.clear()
            self.current_index = -1

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
        def __init__(self):
            self.windows = [
                {"hwnd": 1, "title": "EVE - Pilot A", "x": 0, "y": 0, "w": 800, "h": 600},
                {"hwnd": 2, "title": "EVE - Pilot B", "x": 20, "y": 30, "w": 1000, "h": 800},
            ]

        def list_eve_windows(self, keyword):
            assert keyword == "EVE -"
            return list(self.windows)

        def get_window_info(self, hwnd):
            return next(window for window in self.windows if window["hwnd"] == hwnd)

        def select_window(self, *args, **kwargs):
            self.selected = (args, kwargs)

        def get_member_list_region(self, window):
            return {"x": window["x"], "y": window["y"], "w": 200, "h": window["h"]}

    class FakeLabel:
        def setText(self, text):
            self.text = text

    window = MainWindow.__new__(MainWindow)
    window._settings = type("Settings", (), {"get_keyword": lambda self: "EVE -"})()
    window._capturer = FakeCapturer()
    window._window_combo = FakeCombo()
    window._window_label = FakeLabel()
    window._region_prefs = type("Prefs", (), {"resolve_region": lambda self, item: None})()
    window._refresh_status_cards = lambda: None
    window._refresh_window_status_table = lambda: None
    window._log_message = lambda message: None

    MainWindow._detect_window(window)
    window._window_combo.setCurrentIndex(1)
    window._capturer.windows.append(
        {"hwnd": 3, "title": "EVE - Pilot C", "x": 40, "y": 50, "w": 1200, "h": 900}
    )

    MainWindow._refresh_detected_windows(window)

    assert [data for _label, data in window._window_combo.items] == [1, 2, 3]
    assert window._window_combo.currentData() == 2


def test_refresh_remaps_running_worker_after_window_move_and_resize():
    previous_window = {
        "hwnd": 7,
        "title": "EVE - Pilot",
        "x": 0,
        "y": 0,
        "w": 1000,
        "h": 800,
        "monitor": r"\\.\DISPLAY1",
    }
    current_window = {
        "hwnd": 7,
        "title": "EVE - Pilot",
        "x": 2100,
        "y": 100,
        "w": 2000,
        "h": 1600,
        "monitor": r"\\.\DISPLAY2",
    }

    class FakeWorker:
        def __init__(self):
            self.region = None

        def set_region(self, x, y, w, h):
            self.region = (x, y, w, h)

    class FakePrefs:
        def resolve_region(self, window):
            assert window == current_window
            return {"x": 3600, "y": 260, "w": 400, "h": 1200}

    class FakeCombo:
        def currentData(self):
            return 7

    class FakeController:
        def __init__(self):
            self.anchor = None

        def set_anchor_window(self, window):
            self.anchor = dict(window)

    worker = FakeWorker()
    controller = FakeController()
    context = {
        "window": dict(previous_window),
        "region": {"x": 750, "y": 80, "w": 200, "h": 600},
    }
    window = MainWindow.__new__(MainWindow)
    window._workers = {"target": worker}
    window._worker_contexts = {"target": context}
    window._region_prefs = FakePrefs()
    window._capturer = type(
        "Capturer",
        (),
        {"get_member_list_region": lambda self, item: None},
    )()
    window._window_combo = FakeCombo()
    window._alert_controller = controller

    MainWindow._sync_monitor_target_geometry(window, [current_window])

    assert worker.region == (3600, 260, 400, 1200)
    assert context["window"] == current_window
    assert context["region"] == {
        "x": 3600,
        "y": 260,
        "w": 400,
        "h": 1200,
    }
    assert controller.anchor == current_window


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
    assert table.resized is False


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


def test_start_monitor_rejects_missing_eve_windows(monkeypatch):
    class FakeButton:
        def __init__(self):
            self.checked = True

        def setChecked(self, value):
            self.checked = value

    messages = []
    monkeypatch.setattr(
        "app.ui.main_window.QMessageBox.critical",
        lambda _parent, _title, message: messages.append(message),
    )
    window = MainWindow.__new__(MainWindow)
    window._build_monitor_targets = lambda: []
    window._detect_window = lambda: None
    window._monitor_btn = FakeButton()

    MainWindow._start_monitor(window)

    assert window._monitor_btn.checked is False
    assert messages == ["当前没有可用的 EVE 窗口。"]


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


def test_scan_interval_changes_apply_to_running_workers():
    class FakeWorker:
        def set_interval(self, value):
            self.interval = value

    worker = FakeWorker()
    window = MainWindow.__new__(MainWindow)
    window._settings = type("Settings", (), {"get_interval": lambda self: 5.0})()
    window._running_workers = lambda: [worker]
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._refresh_status_cards = lambda: None

    MainWindow._apply_scan_settings(window)

    assert worker.interval == 5.0
    assert window._log_messages == ["扫描间隔已实时更新为 5 秒"]


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
            "character_name": "Pilot A",
            "system_name": "S-KSWL",
            "system_id": None,
            "system_source": "chatlog",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
        },
        "eve - pilot b": {
            "key": "eve - pilot b",
            "client_id": "detector-client:test:eve-pilot-b",
            "source_instance": "EVE - Pilot B",
            "window_title": "EVE - Pilot B",
            "character_name": "Pilot B",
            "system_name": "HB-FSO",
            "system_id": None,
            "system_source": "chatlog",
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
            "character_name": "Pilot A",
            "system_name": "S-KSWL",
            "system_id": None,
            "system_source": "chatlog",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
            "monitoring": True,
        },
        {
            "client_id": "detector-client:test:eve-pilot-b",
            "window_title": "EVE - Pilot B",
            "source_instance": "EVE - Pilot B",
            "character_name": "Pilot B",
            "system_name": "HB-FSO",
            "system_id": None,
            "system_source": "chatlog",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
            "monitoring": True,
        },
    ]


def test_stopped_monitor_does_not_publish_ocr_or_heartbeat():
    class FailingClient:
        def post_ocr_snapshot(self, **_payload):
            raise AssertionError("stopped monitor uploaded OCR")

        def post_heartbeat(self, **_payload):
            raise AssertionError("stopped monitor uploaded heartbeat")

    window = MainWindow.__new__(MainWindow)
    window._intel_client = FailingClient()
    window._uploads_enabled = False

    MainWindow._publish_ocr_snapshot(window, ["Alice"])
    MainWindow._publish_heartbeat(window)


def test_stop_monitor_disables_timer_and_queues_without_uploading():
    class FakeTimer:
        def __init__(self):
            self.active = True
            self.stop_calls = 0

        def isActive(self):
            return self.active

        def start(self):
            self.active = True

        def stop(self):
            self.active = False
            self.stop_calls += 1

    class FakeNetworkTasks:
        def __init__(self):
            self.cancel_calls = 0

        def cancel_latest(self):
            self.cancel_calls += 1

    class FakeButton:
        def setText(self, value):
            self.text = value

        def setStyleSheet(self, value):
            self.style = value

    class FakeLabel(FakeButton):
        pass

    window = MainWindow.__new__(MainWindow)
    window._uploads_enabled = True
    window._heartbeat_timer = FakeTimer()
    window._network_tasks = FakeNetworkTasks()
    window._stop_monitor_workers = lambda timeout_ms: timeout_ms == 3000
    window._monitor_btn = FakeButton()
    window._status_label = FakeLabel()
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._heartbeat_last_action = "running"
    window._heartbeat_last_success_at = "previous-success"
    window._refresh_status_cards = lambda: None
    heartbeat_calls = []
    window._publish_heartbeat = lambda: heartbeat_calls.append(True)

    MainWindow._stop_monitor(window)

    assert window._uploads_enabled is False
    assert window._heartbeat_timer.active is False
    assert window._heartbeat_timer.stop_calls == 1
    assert window._network_tasks.cancel_calls == 1
    assert window._monitor_btn.text == "开始监控"
    assert window._status_label.text == "已停止"
    assert window._heartbeat_last_action == "monitor_stopped"
    assert window._heartbeat_last_success_at == "previous-success"
    assert heartbeat_calls == []
    assert window._log_messages == ["监控已停止"]


def test_stop_monitor_workers_stops_all_workers_and_clears_context():
    class FakeSignal:
        def __init__(self):
            self.disconnects = 0

        def disconnect(self):
            self.disconnects += 1

    class FakeWorker:
        def __init__(self):
            self.ocr_snapshot = FakeSignal()
            self.hostile_detected = FakeSignal()
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
    assert first.ocr_snapshot.disconnects == 1
    assert first.hostile_detected.disconnects == 1
    assert first.status_update.disconnects == 1
    assert first.scan_complete.disconnects == 1
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


def test_region_selected_updates_running_worker_region():
    class FakePrefs:
        def __init__(self):
            self.saved = None

        def save_region(self, window, region):
            self.saved = (dict(window), dict(region))

    class FakeWorker:
        def __init__(self):
            self.region = None

        def set_region(self, x, y, w, h):
            self.region = {"x": x, "y": y, "w": w, "h": h}

    window_info = {
        "hwnd": 99,
        "title": "EVE - Hajimi6",
        "x": 100,
        "y": 200,
        "w": 800,
        "h": 600,
    }
    key = "hwnd:99:eve - hajimi6"
    worker = FakeWorker()
    context = {"key": key, "region": {"x": 1, "y": 2, "w": 3, "h": 4}}
    updates = []
    heartbeats = []

    window = MainWindow.__new__(MainWindow)
    window._manual_region = None
    window._current_window_info = lambda: window_info
    window._region_prefs = FakePrefs()
    window._workers = {key: worker}
    window._worker_contexts = {key: context}
    window._heartbeat_last_action = ""
    window._heartbeat_last_success_at = ""
    window._publish_heartbeat = lambda: heartbeats.append(True)
    window._update_window_status = (
        lambda item, status, action: updates.append((item, status, action))
    )
    window._window_label = type("Label", (), {"setText": lambda self, text: None})()
    window._log_message = lambda message: None
    window._refresh_status_cards = lambda: None
    window._refresh_window_status_table = lambda: None
    window.show = lambda: None

    MainWindow._on_region_selected(window, 336, 223, 179, 762)

    assert worker.region == {"x": 336, "y": 223, "w": 179, "h": 762}
    assert context["region"] == {"x": 336, "y": 223, "w": 179, "h": 762}
    assert window._region_prefs.saved == (
        window_info,
        {"x": 336, "y": 223, "w": 179, "h": 762},
    )
    assert updates == [(context, "运行中", "区域已更新")]
    assert window._heartbeat_last_action == "region_updated"
    assert heartbeats == [True]
