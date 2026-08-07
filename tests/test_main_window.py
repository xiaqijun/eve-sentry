import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QStyle,
    QStyleOptionSpinBox,
    QSystemTrayIcon,
    QToolButton,
    QWidget,
)

from app.channels.identity_logs import IdentityScanResult
from app.ui.main_window import MainWindow, PreviewCaptureWorker
from app.ui.settings import SettingsPanel
from app.ui.settings import DEFAULT_CHATLOG_DIR
from app.ui.settings import SETTINGS_INLINE_INPUT_WIDTH
from app.ui.settings import SETTINGS_INPUT_HEIGHT
from app.ui.settings import SETTINGS_LONG_INPUT_WIDTH
from app.ui.settings import SETTINGS_NUMBER_INPUT_WIDTH
from app.ui.theme import APP_QSS

_QT_APP = None

def qt_app():
    global _QT_APP
    _QT_APP = QApplication.instance() or _QT_APP or QApplication([])
    return _QT_APP


class FakeCheckAction:
    def __init__(self, checked=False, text=""):
        self._checked = checked
        self._text = text

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = bool(checked)

    def text(self):
        return self._text


def test_setup_tray_keeps_context_menu_alive(tmp_path):
    qt_app()

    class TrayHost(QWidget):
        def __init__(self):
            super().__init__()
            self._settings = SettingsPanel(
                config_path=tmp_path / "tray-settings.json"
            )

        def _on_tray_activated(self, _reason):
            pass

        def _quit_app(self):
            pass

        def _sync_tray_behavior_actions(self):
            MainWindow._sync_tray_behavior_actions(self)

    host = TrayHost()
    MainWindow._setup_tray(host)

    assert host._tray.contextMenu() is None
    assert host._tray_menu.parent() is host
    assert [
        action.text()
        for action in host._tray_menu.actions()
        if not action.isSeparator()
    ] == [
        "显示主窗口",
        "开机启动",
        "启动后最小化",
        "关闭到托盘",
        "恢复上次监控状态",
        "退出",
    ]
    start_minimized = host._tray_behavior_actions["start_minimized"]
    start_minimized.setChecked(True)
    assert host._settings.get_start_minimized() is True
    host._settings.set_behavior_preference("start_minimized", False)
    host._sync_tray_behavior_actions()
    assert start_minimized.isChecked() is False
    host._tray.hide()


def test_tray_context_activation_explicitly_opens_menu():
    calls = []

    class FakeMenu:
        def exec(self, position):
            calls.append(position)

    window = SimpleNamespace(_tray_menu=FakeMenu())

    MainWindow._on_tray_activated(
        window,
        QSystemTrayIcon.ActivationReason.Context,
    )

    assert len(calls) == 1


def test_close_event_starts_shutdown_without_accepting_a_blocking_close():
    calls = []

    class FakeEvent:
        def ignore(self):
            calls.append("ignored")

    class FakeWindow:
        def _quit_app(self):
            calls.append("shutdown")

    MainWindow.closeEvent(FakeWindow(), FakeEvent())

    assert calls == ["shutdown", "ignored"]


def test_close_event_installs_ready_update_instead_of_hiding_to_tray():
    calls = []

    class FakeEvent:
        def ignore(self):
            calls.append("ignored")

    class FakeSettings:
        def get_close_to_tray(self):
            return True

    class FakeUpdater:
        ready_to_install = True

    class FakeWindow:
        def __init__(self):
            self._settings = FakeSettings()
            self._updater = FakeUpdater()
            self._shutdown_in_progress = False

        def _quit_app(self):
            calls.append("shutdown")

        def hide(self):
            calls.append("hidden")

    MainWindow.closeEvent(FakeWindow(), FakeEvent())

    assert calls == ["shutdown", "ignored"]


def test_close_event_still_hides_to_tray_without_ready_update():
    calls = []

    class FakeEvent:
        def ignore(self):
            calls.append("ignored")

    class FakeSettings:
        def get_close_to_tray(self):
            return True

    class FakeUpdater:
        ready_to_install = False

    class FakeTray:
        def showMessage(self, title, message):
            calls.append((title, message))

    class FakeWindow:
        def __init__(self):
            self._settings = FakeSettings()
            self._updater = FakeUpdater()
            self._tray = FakeTray()
            self._shutdown_in_progress = False

        def _quit_app(self):
            calls.append("shutdown")

        def hide(self):
            calls.append("hidden")

    MainWindow.closeEvent(FakeWindow(), FakeEvent())

    assert calls == [
        "hidden",
        ("EVE Sentry", "客户端仍在托盘中运行"),
        "ignored",
    ]


def test_quit_app_hides_and_starts_non_blocking_worker_shutdown(monkeypatch):
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

        def hide(self):
            calls.append("window")

        def _stop_alert(self, *, wait_for_worker=False):
            calls.append(("alert", wait_for_worker))

        def _stop_monitor(self, *, wait_for_workers=False):
            calls.append(("monitor", wait_for_workers))

        def _finish_quit_when_workers_stop(self):
            calls.append("poll")

    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda delay, callback: (calls.append(("timer", delay)), callback()),
    )

    MainWindow._quit_app(FakeWindow())

    assert calls == [
        "window",
        "tray",
        ("monitor", False),
        ("alert", False),
        "network",
        ("timer", 0),
        "poll",
    ]


def test_quit_app_stays_open_when_ready_update_cannot_launch():
    calls = []

    class FakeUpdater:
        ready_to_install = True

        def install_on_exit(self):
            calls.append("install")
            return False

    class FakeWindow:
        def __init__(self):
            self._updater = FakeUpdater()
            self._shutdown_in_progress = False

        def show(self):
            calls.append("show")

        def raise_(self):
            calls.append("raise")

        def hide(self):
            calls.append("hide")

    window = FakeWindow()

    MainWindow._quit_app(window)

    assert calls == ["install", "show", "raise"]
    assert window._shutdown_in_progress is False


def test_shutdown_poll_quits_only_after_qt_workers_exit(monkeypatch):
    callbacks = []
    calls = []

    class FakeWorker:
        running = True

        def isRunning(self):
            return self.running

    class FakeController:
        running = True

        def is_running(self):
            return self.running

    class FakeWindow:
        def __init__(self):
            self.worker = FakeWorker()
            self.controller = FakeController()
            self._stopping_monitor_workers = {self.worker}
            self._stopping_alert_controllers = {self.controller}
            self._finish_quit_when_workers_stop = (
                lambda: MainWindow._finish_quit_when_workers_stop(self)
            )

    window = FakeWindow()
    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda delay, callback: callbacks.append((delay, callback)),
    )
    monkeypatch.setattr(QApplication, "quit", lambda: calls.append("quit"))

    MainWindow._finish_quit_when_workers_stop(window)

    assert len(callbacks) == 1
    assert callbacks[0][0] == 50
    assert calls == []

    window.worker.running = False
    window.controller.running = False
    callbacks.pop()[1]()

    assert calls == ["quit"]


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

    MainWindow._start_alert(window, identity_checked=True)

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


def test_key_validation_is_independent_from_listener_scan():
    class FakeScanner:
        def __init__(self):
            self.key_validated = False
            self.marked = 0

        def scan(self, _api_key):
            return SimpleNamespace(
                pending_characters=[],
                pending_files=["Local_new.txt"],
                processed_count=0,
                key_validated=self.key_validated,
                identity_verified=False,
            )

        def mark_key_validated(self):
            self.key_validated = True
            self.marked += 1

    class FakeClient:
        def __init__(self):
            self.validation_calls = 0

        def validate_api_key(self):
            self.validation_calls += 1
            return {"user_id": "user-1"}

    class FakeStore:
        def load(self):
            return {"characters": [], "key_validated": True}

    class FakeSettings:
        def auth_state_store(self):
            return FakeStore()

    window = MainWindow.__new__(MainWindow)
    window._identity_scanner = FakeScanner()
    window._settings = FakeSettings()
    client = FakeClient()

    MainWindow._validate_api_key(window, client)
    MainWindow._validate_api_key(window, client)
    scan = MainWindow._scan_and_validate_identities(window, client, "eve_valid")

    assert scan["characters"] == []
    assert scan["pending_files"] == ["Local_new.txt"]
    assert client.validation_calls == 2
    assert window._identity_scanner.marked == 2


def test_identity_check_submits_listener_found_after_key_validation():
    class FakeScanner:
        def __init__(self):
            self.verified = []

        def scan(self, _api_key):
            return SimpleNamespace(
                pending_characters=["Alice"],
                pending_files=[],
                processed_count=1,
                key_validated=True,
                identity_verified=False,
            )

        def mark_verified(self, names):
            self.verified = list(names)

    class FakeClient:
        def __init__(self):
            self.names = []

        def verify_eve_characters(self, names):
            self.names = list(names)
            return {"verified": True, "permanent": True}

    class FakeStore:
        def load(self):
            return {"characters": ["Alice"], "key_validated": True}

    class FakeSettings:
        def auth_state_store(self):
            return FakeStore()

    window = MainWindow.__new__(MainWindow)
    window._identity_scanner = FakeScanner()
    window._settings = FakeSettings()
    client = FakeClient()

    result = MainWindow._scan_and_validate_identities(window, client, "eve_valid")

    assert result["characters"] == ["Alice"]
    assert client.names == ["Alice"]
    assert window._identity_scanner.verified == ["Alice"]


def test_identity_check_backfills_and_remembers_missing_character_id():
    remembered = []

    class FakeScanner:
        def scan(self, _api_key):
            return SimpleNamespace(
                pending_characters=[],
                pending_files=[],
                processed_count=0,
                key_validated=True,
                identity_verified=True,
            )

        def mark_verified(self, names):
            assert names == ["Alice"]

    class FakeClient:
        def verify_eve_characters(self, names):
            assert names == ["Alice"]
            return {
                "verified": True,
                "permanent": True,
                "characters": [
                    {"character_id": 101, "character_name": "Alice"},
                ],
            }

    class FakeStore:
        def load(self):
            return {
                "characters": ["Alice"],
                "character_identities": [],
            }

        def remember_character_identities(self, characters):
            remembered.extend(characters)

    store = FakeStore()
    window = MainWindow.__new__(MainWindow)
    window._identity_scanner = FakeScanner()
    window._settings = type(
        "Settings",
        (),
        {"auth_state_store": lambda self: store},
    )()

    MainWindow._scan_and_validate_identities(
        window,
        FakeClient(),
        "eve_valid",
    )

    assert remembered == [
        {"character_id": 101, "character_name": "Alice"},
    ]


def test_async_identity_report_keeps_pending_names_until_server_verifies():
    verified = []
    remembered = []

    class FakeScanner:
        def scan(self, _api_key):
            return IdentityScanResult(
                characters=["Alice"], pending_characters=["Alice"],
                pending_files=[], processed_count=0, initial_scan=False,
                key_validated=True, identity_verified=False,
            )

        def mark_verified(self, names):
            verified.append(list(names))

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def ensure_eve_character_check(self, names, client_id=""):
            self.calls += 1
            assert names == ["Alice"]
            assert client_id == "detector:test"
            if self.calls == 1:
                return {"accepted": True, "status": "processing", "pending": True}
            return {
                "accepted": True, "status": "verified", "pending": False,
                "verified": True,
                "characters": [{"character_id": 101, "character_name": "Alice"}],
            }

    class FakeStore:
        def load(self):
            return {"characters": ["Alice"], "character_identities": []}

        def remember_character_identities(self, characters):
            remembered.extend(characters)

    store = FakeStore()
    window = MainWindow.__new__(MainWindow)
    window._identity_scanner = FakeScanner()
    window._heartbeat_client_id = "detector:test"
    window._settings = SimpleNamespace(auth_state_store=lambda: store)
    client = FakeClient()

    first = MainWindow._scan_and_validate_identities(window, client, "eve_valid")
    assert first["identity"]["pending"] is True
    assert verified == []
    assert remembered == []

    second = MainWindow._scan_and_validate_identities(window, client, "eve_valid")
    assert second["identity"]["verified"] is True
    assert verified == [["Alice"]]
    assert remembered == [{"character_id": 101, "character_name": "Alice"}]


def test_listener_poll_rediscovers_log_path_and_runs_as_silent_task():
    submissions = []

    class FakeSettings:
        def get_api_key(self):
            return "eve_valid"

        def get_channel_log_dir(self):
            return "D:/New/EVE/logs/Chatlogs"

    class FakeScanner:
        log_dir = Path("C:/Old/Chatlogs")

    class FakeRunner:
        def submit_latest(self, key, task, metadata):
            submissions.append((key, task, metadata))
            return True

    window = MainWindow.__new__(MainWindow)
    window._settings = FakeSettings()
    window._identity_scanner = FakeScanner()
    window._intel_client = object()
    window._network_tasks = FakeRunner()

    MainWindow._poll_identity_logs(window)

    assert window._identity_scanner.log_dir == Path("D:/New/EVE/logs/Chatlogs")
    assert window._listener_scan_running is True
    assert submissions[0][0] == "listener"
    assert submissions[0][2] == {"kind": "listener"}


def test_listener_background_errors_only_disable_features_for_auth_rejection():
    disabled = []
    window = MainWindow.__new__(MainWindow)
    window._listener_scan_running = True
    window._disable_authenticated_features = disabled.append

    MainWindow._handle_listener_scan_error(window, TimeoutError("ESI timed out"))

    assert window._listener_scan_running is False
    assert disabled == []

    window._listener_scan_running = True
    MainWindow._handle_listener_scan_error(
        window,
        RuntimeError("API key is invalid or revoked"),
    )

    assert window._listener_scan_running is False
    assert disabled == ["API key is invalid or revoked"]


def test_identity_success_displays_success_without_character_count():
    class FakeButton:
        def __init__(self):
            self.enabled = False

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setChecked(self, checked):
            self.checked = checked

    class FakeSettings:
        def __init__(self):
            self.auth_status = None

        def set_auth_status(self, message):
            self.auth_status = message

    window = MainWindow.__new__(MainWindow)
    window._identity_check_running = True
    window._identity_wants_monitor = False
    window._identity_wants_alert = False
    window._monitor_btn = FakeButton()
    window._alert_btn = FakeButton()
    window._settings = FakeSettings()
    window._alert_controller = None
    window._is_monitoring = lambda: False
    window._start_monitor = lambda identity_checked=False: None
    window._log_message = lambda _message: None

    MainWindow._handle_identity_check_success(
        window,
        {"characters": ["Alice", "Bob"], "processed_count": 0},
        {"action": "monitor"},
    )

    assert window._settings.auth_status == "认证成功"
    assert window._api_key_validated is True
    assert window._monitor_btn.enabled is True
    assert window._alert_btn.enabled is True


def test_cached_key_validation_starts_monitor_without_network_wait():
    class FakeSettings:
        def get_api_key(self):
            return "eve_valid"

    started = []
    window = MainWindow.__new__(MainWindow)
    window._settings = FakeSettings()
    window._intel_client = object()
    window._api_key_validated = True
    window._identity_check_running = False
    window._identity_wants_monitor = False
    window._identity_wants_alert = False
    window._start_monitor = lambda identity_checked=False: started.append(identity_checked)

    MainWindow._begin_identity_check(window, "monitor")

    assert started == [True]
    assert window._identity_wants_monitor is False


def test_empty_key_starts_monitor_and_alert_without_authentication():
    class FakeSettings:
        def __init__(self):
            self.auth_status = ""

        def get_api_key(self):
            return ""

        def set_auth_status(self, message, error=False):
            assert error is False
            self.auth_status = message

    class FailingNetworkTasks:
        def submit_latest(self, *_args, **_kwargs):
            raise AssertionError("empty credentials must not start identity validation")

    window = MainWindow.__new__(MainWindow)
    window._settings = FakeSettings()
    window._intel_client = object()
    window._network_tasks = FailingNetworkTasks()
    window._identity_check_running = False
    window._identity_wants_monitor = False
    window._identity_wants_alert = False
    window._api_key_validated = True
    started = []
    window._start_monitor = (
        lambda identity_checked=False: started.append(("monitor", identity_checked))
    )
    window._start_alert = (
        lambda identity_checked=False: started.append(("alert", identity_checked))
    )

    MainWindow._begin_identity_check(window, "monitor")
    MainWindow._begin_identity_check(window, "alert")

    assert started == [("monitor", True), ("alert", True)]
    assert window._settings.auth_status == "未启用认证"
    assert window._api_key_validated is False
    assert window._identity_wants_monitor is False
    assert window._identity_wants_alert is False


def test_empty_server_starts_local_monitor_without_identity_request():
    class FakeSettings:
        def __init__(self):
            self.auth_status = ""

        def get_api_key(self):
            return "eve_configured"

        def set_auth_status(self, message, error=False):
            assert error is False
            self.auth_status = message

    window = MainWindow.__new__(MainWindow)
    window._settings = FakeSettings()
    window._intel_url = ""
    window._intel_client = None
    window._identity_check_running = False
    window._identity_wants_monitor = False
    window._identity_wants_alert = False
    window._api_key_validated = True
    started = []
    window._start_monitor = (
        lambda identity_checked=False: started.append(identity_checked)
    )

    MainWindow._begin_identity_check(window, "monitor")

    assert started == [True]
    assert window._settings.auth_status == "未配置服务端"
    assert window._api_key_validated is False


def test_auth_rejection_clears_cached_key_validation():
    class FakeButton:
        def setChecked(self, _checked):
            return None

    class FakeSettings:
        def set_auth_status(self, _message, error=False):
            return None

    window = MainWindow.__new__(MainWindow)
    window._api_key_validated = True
    window._identity_wants_monitor = False
    window._identity_wants_alert = False
    window._is_monitoring = lambda: False
    window._alert_controller = None
    window._monitor_btn = FakeButton()
    window._alert_btn = FakeButton()
    window._settings = FakeSettings()
    window._log_message = lambda _message: None

    MainWindow._disable_authenticated_features(window, "API key is invalid or revoked")

    assert window._api_key_validated is False


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


def test_window_system_change_refreshes_local_alert_systems(monkeypatch):
    class Detection:
        system_name = "HB-FSO"

    class Controller:
        def __init__(self):
            self.systems = []

        def show_monitoring_systems(self, systems):
            self.systems.append(list(systems))

    monkeypatch.setattr(
        "app.ui.main_window.find_latest_local_system",
        lambda log_dir, character_name="": (
            Detection()
            if log_dir == "C:/EVE/Chatlogs" and character_name == "Pilot A"
            else None
        ),
    )
    context = {
        "key": "pilot-a",
        "character_name": "Pilot A",
        "window": {"hwnd": 7},
        "system_name": "S-KSWL",
        "system_source": "chatlog",
        "_location_next_check": 0.0,
    }
    window = MainWindow.__new__(MainWindow)
    window._use_local_system_log = True
    window._settings = type(
        "Settings",
        (),
        {"get_channel_log_dir": lambda self: "C:/EVE/Chatlogs"},
    )()
    window._location_refresh_ttl = 5.0
    window._last_local_system_error = ""
    window._heartbeat_last_action = ""
    window._heartbeat_last_success_at = ""
    window._heartbeat_last_error = ""
    window._intel_system = "S-KSWL"
    window._intel_system_id = None
    window._intel_system_source = "chatlog"
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 7},
    )()
    window._worker_contexts = {"pilot-a": context}
    window._workers = {"pilot-a": object()}
    window._alert_controller = Controller()
    window._log_message = lambda _message: None
    window._refresh_status_cards = lambda: None

    assert MainWindow._refresh_intel_location(
        window,
        force=True,
        context=context,
    ) is True

    assert context["system_name"] == "HB-FSO"
    assert window._intel_system == "HB-FSO"
    assert window._alert_controller.systems == [["HB-FSO"]]


def test_initial_local_system_promotes_first_detected_window(monkeypatch):
    class Detection:
        system_name = "Jita"

    monkeypatch.setattr(
        "app.ui.main_window.find_latest_local_system",
        lambda _log_dir, character_name="": Detection()
        if character_name == "Pilot A"
        else None,
    )
    context = {
        "character_name": "Pilot A",
        "window": {"hwnd": 7},
        "system_name": "Unknown",
        "system_source": "default",
        "_location_next_check": 0.0,
    }
    window = MainWindow.__new__(MainWindow)
    window._use_local_system_log = True
    window._settings = type(
        "Settings",
        (),
        {"get_channel_log_dir": lambda self: "C:/EVE/Chatlogs"},
    )()
    window._location_refresh_ttl = 5.0
    window._last_local_system_error = ""
    window._heartbeat_last_action = ""
    window._heartbeat_last_success_at = ""
    window._heartbeat_last_error = ""
    window._intel_system = "Unknown"
    window._intel_system_source = "default"
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: None},
    )()
    window._alert_controller = None
    window._log_message = lambda _message: None
    window._refresh_status_cards = lambda: None

    assert MainWindow._refresh_intel_location(
        window,
        force=True,
        context=context,
    ) is True
    assert window._intel_system == "Jita"
    assert window._intel_system_source == "chatlog"


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
        "ocr_enabled": True,
        "window_keyword": "EVE - Pilot",
        "server_url": "",
        "start_with_windows": False,
        "start_minimized": False,
        "close_to_tray": True,
        "restore_monitor_state": True,
        "alert_sound_enabled": True,
        "alert_repeat_interval": 2,
        "alert_repeat_count": 3,
    }

    assert not hasattr(panel, "_alert_volume_spin")
    assert not hasattr(panel, "_alert_cooldown_spin")
    assert not hasattr(panel, "_quiet_hours_edit")
    assert not hasattr(panel, "_alert_severity_combo")
    assert all(
        widget.height() == SETTINGS_INPUT_HEIGHT
        for widget in (
            panel._server_url_edit,
            panel._api_key_edit,
            panel._interval_spin,
            panel._keyword_edit,
            panel._alert_repeat_interval_spin,
            panel._alert_repeat_count_spin,
        )
    )
    assert panel._server_url_edit.width() == SETTINGS_LONG_INPUT_WIDTH
    assert panel._api_key_edit.width() == SETTINGS_LONG_INPUT_WIDTH
    assert panel._api_key_edit.maxLength() == 128
    assert panel._keyword_edit.width() == SETTINGS_INLINE_INPUT_WIDTH
    assert all(
        widget.width() == SETTINGS_NUMBER_INPUT_WIDTH
        for widget in (
            panel._interval_spin,
            panel._alert_repeat_interval_spin,
            panel._alert_repeat_count_spin,
        )
    )
    assert all(
        widget.suffix() == ""
        for widget in (
            panel._interval_spin,
            panel._alert_repeat_interval_spin,
            panel._alert_repeat_count_spin,
        )
    )
    assert [
        label.text() for label in panel.findChildren(QLabel, "inputUnit")
    ] == ["秒", "秒", "次"]
    assert panel.get_alert_preferences() == {
        "muted": False,
        "volume": 1.0,
        "repeat_interval": 2.0,
        "repeat_count": 3,
    }
    assert panel._behavior_group.isHidden()


def test_settings_panel_rejects_multiline_key_and_redacts_header_error(tmp_path):
    qt_app()
    panel = SettingsPanel(config_path=tmp_path / "channel_settings.json")
    validator = panel._api_key_edit.validator()

    valid_state, _, _ = validator.validate("eve_valid_key-123", 0)
    invalid_state, _, _ = validator.validate("Vargur\tCargo Hold\nDrone Bay", 0)

    assert valid_state == validator.State.Acceptable
    assert invalid_state == validator.State.Invalid

    panel.set_auth_status(
        "Illegal header value b'Bearer eve_secret\\nCargo Hold'",
        error=True,
    )

    assert panel._auth_status_label.text() == "设备密钥格式无效，请重新复制完整密钥"
    assert "eve_secret" not in panel._auth_status_label.text()


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


def test_settings_panel_migrates_legacy_muted_alert_setting(tmp_path):
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps(
            {
                "alert_muted": True,
                "quiet_hours": "23:00-07:00",
                "alert_min_severity": "critical",
            }
        ),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel._alert_sound_check.isChecked() is False
    assert panel.get_alert_preferences()["muted"] is True
    panel.save_channel_config()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["alert_sound_enabled"] is False
    assert saved["alert_repeat_interval"] == 2
    assert saved["alert_repeat_count"] == 3
    assert "alert_muted" not in saved
    assert "quiet_hours" not in saved
    assert "alert_min_severity" not in saved


def test_settings_panel_rediscovers_active_chatlog_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_CHATLOG_DIR", raising=False)
    calls = []

    def resolve(preferred=None):
        calls.append(str(preferred))
        return Path("C:/Old/Chatlogs" if len(calls) == 1 else "D:/New/Chatlogs")

    monkeypatch.setattr("app.ui.settings.resolve_chatlog_dir", resolve)
    qt_app()
    panel = SettingsPanel(config_path=tmp_path / "channel_settings.json")

    assert panel.get_channel_log_dir() == str(Path("D:/New/Chatlogs"))
    assert calls == [str(DEFAULT_CHATLOG_DIR), str(Path("C:/Old/Chatlogs"))]


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


def test_settings_panel_does_not_supply_a_default_server_url(tmp_path, monkeypatch):
    monkeypatch.delenv("EVE_SENTRY_INTEL_URL", raising=False)
    qt_app()
    panel = SettingsPanel(config_path=tmp_path / "channel_settings.json")

    assert panel.get_server_url() == ""
    assert "127.0.0.1" not in panel._server_url_edit.placeholderText()
    assert "114.132.167.239" not in panel._server_url_edit.placeholderText()


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


def test_settings_panel_empty_environment_keeps_saved_server_url(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("EVE_SENTRY_INTEL_URL", "   ")
    config_path = tmp_path / "channel_settings.json"
    config_path.write_text(
        json.dumps({"server_url": "http://saved.example:8765/"}),
        encoding="utf-8",
    )

    qt_app()
    panel = SettingsPanel(config_path=config_path)

    assert panel.get_server_url() == "http://saved.example:8765"


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


def test_apply_server_url_allows_clearing_the_configured_server():
    window = MainWindow.__new__(MainWindow)
    window._intel_url = "http://old.example"
    window._intel_client = object()
    window._alert_controller = None
    window._uploads_enabled = False
    window._create_intel_client = lambda: None
    window._last_heartbeat_error = "old"
    window._heartbeat_last_error = "old"
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._refresh_status_cards = lambda: None

    MainWindow._apply_server_url(window, "")

    assert window._intel_url == ""
    assert window._intel_client is None
    assert window._log_messages == ["服务端地址已清除，网络功能已停用"]


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

    assert up_rect.width() >= 20
    assert down_rect.width() >= 20
    assert up_rect.left() == down_rect.left()
    assert up_rect.right() == spinbox.rect().right()
    assert down_rect.right() == spinbox.rect().right()
    assert up_rect.bottom() < down_rect.bottom()
    assert down_rect.bottom() == spinbox.rect().bottom()

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


def test_start_monitor_creates_worker_for_each_eve_window(monkeypatch):
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
            self.ocr_enabled = None
            self.running = False
            created_workers.append(self)

        def set_window(self, window):
            self.window = dict(window)

        def set_region(self, x, y, w, h):
            self.region = {"x": x, "y": y, "w": w, "h": h}

        def set_interval(self, seconds):
            self.interval = seconds

        def set_ocr_enabled(self, enabled):
            self.ocr_enabled = enabled

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
                {
                    "hwnd": 1,
                    "title": "EVE - Pilot A",
                    "x": 10,
                    "y": 20,
                    "w": 1000,
                    "h": 800,
                },
                {
                    "hwnd": 2,
                    "title": "EVE - Pilot B",
                    "x": 20,
                    "y": 30,
                    "w": 1000,
                    "h": 800,
                },
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
            return {
                "x": window["x"] + window["w"] - 200,
                "y": window["y"],
                "w": 200,
                "h": window["h"],
            }

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
            "get_ocr_enabled": lambda self: True,
        },
    )()
    window._region_prefs = FakeRegionPrefs()
    window._heartbeat_client_id = "detector-client:test"
    window._monitor_window_actions = {
        "hwnd:1:eve - pilot a": FakeCheckAction(True),
        "hwnd:2:eve - pilot b": FakeCheckAction(True),
    }
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 2},
    )()
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
    window._ocr = object()
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

    MainWindow._start_monitor(window, identity_checked=True)

    assert len(created_workers) == 2
    assert all(worker.ocr is window._ocr for worker in created_workers)
    assert [worker.window["title"] for worker in created_workers] == [
        "EVE - Pilot B",
        "EVE - Pilot A",
    ]
    assert [worker.region for worker in created_workers] == [
        {"x": 760, "y": 190, "w": 220, "h": 420},
        {"x": 810, "y": 20, "w": 200, "h": 800},
    ]
    assert all(worker.interval == 2.0 for worker in created_workers)
    assert all(worker.ocr_enabled is True for worker in created_workers)
    assert set(window._workers) == {
        "hwnd:1:eve - pilot a",
        "hwnd:2:eve - pilot b",
    }
    assert {
        context["client_id"] for context in window._worker_contexts.values()
    } == {
        "detector-client:test:user-pilot-a",
        "detector-client:test:user-pilot-b",
    }
    assert {
        context["source_instance"] for context in window._worker_contexts.values()
    } == {"EVE - Pilot A", "EVE - Pilot B"}
    assert resolved_characters == ["Pilot B", "Pilot A"]


def test_build_monitor_targets_uses_only_selected_window():
    class FakeCapturer:
        def list_eve_windows(self, keyword):
            assert keyword == "EVE -"
            return [
                {
                    "hwnd": 1,
                    "title": "EVE - Pilot A",
                    "x": 10,
                    "y": 20,
                    "w": 1000,
                    "h": 800,
                },
                {
                    "hwnd": 2,
                    "title": "EVE - Pilot B",
                    "x": 20,
                    "y": 30,
                    "w": 1000,
                    "h": 800,
                },
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
            return {
                "x": window["x"] + window["w"] - 200,
                "y": window["y"],
                "w": 200,
                "h": window["h"],
            }

    class FakeStore:
        def load(self):
            return {
                "character_identities": [
                    {"character_id": 202, "character_name": "Pilot B"},
                ],
            }

    window = MainWindow.__new__(MainWindow)
    window._capturer = FakeCapturer()
    window._settings = type(
        "Settings",
        (),
        {
            "get_keyword": lambda self: "EVE -",
            "auth_state_store": lambda self: FakeStore(),
        },
    )()
    window._region_prefs = FakeRegionPrefs()
    window._heartbeat_client_id = "detector-client:test"
    window._monitor_window_actions = {
        "hwnd:1:eve - pilot a": FakeCheckAction(False),
        "hwnd:2:eve - pilot b": FakeCheckAction(True),
    }
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 2},
    )()

    targets = MainWindow._build_monitor_targets(window)

    assert [target["key"] for target in targets] == ["hwnd:2:eve - pilot b"]
    assert [target["client_id"] for target in targets] == [
        "detector-client:test:user-202"
    ]
    assert [target["character_id"] for target in targets] == [202]
    assert [target["source_instance"] for target in targets] == ["EVE - Pilot B"]
    assert [target["character_name"] for target in targets] == ["Pilot B"]


def test_selected_monitor_windows_supports_arbitrary_subset():
    windows = [
        {"hwnd": 1, "title": "EVE - Pilot A"},
        {"hwnd": 2, "title": "EVE - Pilot B"},
        {"hwnd": 3, "title": "EVE - Pilot C"},
    ]
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_actions = {
        "hwnd:1:eve - pilot a": FakeCheckAction(True),
        "hwnd:2:eve - pilot b": FakeCheckAction(False),
        "hwnd:3:eve - pilot c": FakeCheckAction(True),
    }
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 3},
    )()

    selected = MainWindow._selected_monitor_windows(window, windows)

    assert [item["title"] for item in selected] == [
        "EVE - Pilot C",
        "EVE - Pilot A",
    ]


def test_monitor_window_menu_does_not_auto_select_new_windows_in_legacy_all_mode():
    qt_app()

    class FakeRuntimeSettings:
        def __init__(self):
            self.values = {}

        def setValue(self, key, value):
            self.values[key] = value

    windows = [
        {"hwnd": 1, "title": "EVE - Pilot A"},
        {"hwnd": 2, "title": "EVE - Pilot B"},
    ]
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_menu = QMenu()
    window._monitor_window_button = QToolButton()
    window._monitor_window_actions = {}
    window._monitor_windows_by_key = {}
    window._monitor_known_window_keys = set()
    window._monitor_selected_titles = set()
    window._monitor_select_all_new_windows = True
    window._syncing_monitor_menu = False
    window._runtime_settings = FakeRuntimeSettings()
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 1},
    )()
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: False},
    )()

    MainWindow._sync_monitor_window_menu(window, windows)
    assert not any(action.isChecked() for action in window._monitor_window_actions.values())

    window._monitor_window_actions["hwnd:2:eve - pilot b"].setChecked(False)
    MainWindow._on_monitor_window_toggled(window)
    windows = [
        {"hwnd": 11, "title": "EVE - Pilot A"},
        {"hwnd": 2, "title": "EVE - Pilot B"},
        {"hwnd": 3, "title": "EVE - Pilot C"},
    ]
    MainWindow._sync_monitor_window_menu(window, windows)

    assert {
        key: action.isChecked()
        for key, action in window._monitor_window_actions.items()
    } == {
        "hwnd:11:eve - pilot a": False,
        "hwnd:2:eve - pilot b": False,
        "hwnd:3:eve - pilot c": False,
    }
    assert window._monitor_window_button.text() == "监控窗口 0/3"

    MainWindow._select_all_monitor_windows(window)
    windows.append({"hwnd": 4, "title": "EVE - Pilot D"})
    MainWindow._sync_monitor_window_menu(window, windows)

    assert {
        key: action.isChecked()
        for key, action in window._monitor_window_actions.items()
    } == {
        "hwnd:11:eve - pilot a": True,
        "hwnd:2:eve - pilot b": True,
        "hwnd:3:eve - pilot c": True,
        "hwnd:4:eve - pilot d": False,
    }
    assert window._monitor_window_button.text() == "监控窗口 3/4"


def test_monitor_window_menu_handles_untitled_process_window():
    qt_app()
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_menu = QMenu()
    window._monitor_window_button = QToolButton()
    window._monitor_window_actions = {}
    window._monitor_windows_by_key = {}
    window._monitor_known_window_keys = set()
    window._monitor_selected_titles = set()
    window._monitor_select_all_new_windows = True
    window._syncing_monitor_menu = False
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 99},
    )()

    MainWindow._sync_monitor_window_menu(
        window,
        [{"hwnd": 99, "title": ""}],
    )

    assert list(window._monitor_window_actions) == ["hwnd:99"]
    assert window._monitor_window_titles_by_key == {"hwnd:99": "EVE 窗口"}


def test_monitor_window_menu_can_select_only_current_calibration_window():
    qt_app()
    windows = [
        {"hwnd": 1, "title": "EVE - Pilot A"},
        {"hwnd": 2, "title": "EVE - Pilot B"},
        {"hwnd": 3, "title": "EVE - Pilot C"},
    ]
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_menu = QMenu()
    window._monitor_window_button = QToolButton()
    window._monitor_window_actions = {}
    window._monitor_windows_by_key = {}
    window._monitor_known_window_keys = set()
    window._monitor_selected_titles = set()
    window._monitor_select_all_new_windows = True
    window._syncing_monitor_menu = False
    window._runtime_settings = type(
        "Settings",
        (),
        {"setValue": lambda self, key, value: None},
    )()
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 2},
    )()
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: False},
    )()

    MainWindow._sync_monitor_window_menu(window, windows)
    MainWindow._select_current_monitor_window(window)

    assert {
        key: action.isChecked()
        for key, action in window._monitor_window_actions.items()
    } == {
        "hwnd:1:eve - pilot a": False,
        "hwnd:2:eve - pilot b": True,
        "hwnd:3:eve - pilot c": False,
    }
    assert window._monitor_window_button.text() == "监控窗口 1/3"
    assert window._monitor_selected_titles == {"EVE - Pilot B"}


def test_monitor_window_change_rebuilds_running_workers(monkeypatch):
    qt_app()
    callbacks = []
    starts = []
    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda _delay, callback: callbacks.append(callback),
    )
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_actions = {
        "hwnd:1:eve - pilot a": FakeCheckAction(True, "EVE - Pilot A"),
    }
    window._monitor_windows_by_key = {
        "hwnd:1:eve - pilot a": {"hwnd": 1, "title": "EVE - Pilot A"},
    }
    window._monitor_selected_titles = set()
    window._syncing_monitor_menu = False
    window._runtime_settings = type(
        "Settings",
        (),
        {"setValue": lambda self, key, value: None},
    )()
    window._monitor_window_button = QToolButton()
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: True},
    )()
    window._log_message = lambda message: None
    window._start_monitor = lambda **kwargs: starts.append(kwargs)

    MainWindow._on_monitor_window_toggled(window)
    assert len(callbacks) == 1

    callbacks[0]()
    assert starts == [{"identity_checked": True}]


def test_monitor_window_menu_retains_offline_window_selection_memory():
    qt_app()
    windows = [
        {"hwnd": 1, "title": "EVE - Pilot A"},
        {"hwnd": 2, "title": "EVE - Pilot B"},
    ]
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_menu = QMenu()
    window._monitor_window_button = QToolButton()
    window._monitor_window_actions = {}
    window._monitor_windows_by_key = {}
    window._monitor_window_titles_by_key = {}
    window._monitor_last_status_by_title = {}
    window._monitor_known_window_keys = set()
    window._monitor_selected_titles = {
        "EVE - Pilot A",
        "EVE - Pilot C",
    }
    window._monitor_select_all_new_windows = False
    window._syncing_monitor_menu = False
    window._runtime_settings = type(
        "Settings",
        (),
        {"setValue": lambda self, key, value: None},
    )()
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 1},
    )()
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: False},
    )()
    window._worker_contexts = {
        "hwnd:1:eve - pilot a": {
            "window_title": "EVE - Pilot A",
            "character_name": "Pilot A",
            "system_name": "S-KSWL",
            "runtime_status": "扫描中",
        }
    }

    MainWindow._sync_monitor_window_menu(window, windows)

    actions = window._monitor_window_actions
    assert actions["hwnd:1:eve - pilot a"].text() == (
        "Pilot A · S-KSWL · 扫描中"
    )
    assert actions["hwnd:1:eve - pilot a"].isChecked() is True
    assert actions["hwnd:2:eve - pilot b"].text() == (
        "Pilot B · 未知 · 未监控"
    )
    assert "offline:eve - pilot c" not in actions
    assert window._monitor_window_button.text() == "监控窗口 1/2"
    assert window._monitor_window_button.property("selectionState") == "ready"
    assert window._monitor_selected_titles == {
        "EVE - Pilot A",
        "EVE - Pilot C",
    }


def test_monitor_window_button_preserves_selection_when_all_windows_close():
    qt_app()
    window = MainWindow.__new__(MainWindow)
    window._monitor_window_menu = QMenu()
    window._monitor_window_button = QToolButton()
    window._monitor_window_actions = {}
    window._monitor_windows_by_key = {}
    window._monitor_window_titles_by_key = {}
    window._monitor_last_status_by_title = {}
    window._monitor_known_window_keys = set()
    window._monitor_selected_titles = set()
    window._monitor_select_all_new_windows = False
    window._syncing_monitor_menu = False
    window._runtime_settings = type(
        "Settings",
        (),
        {"setValue": lambda self, key, value: None},
    )()
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 1},
    )()
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: False},
    )()
    window._worker_contexts = {}
    windows = [
        {"hwnd": 1, "title": "EVE - Pilot A"},
        {"hwnd": 2, "title": "EVE - Pilot B"},
    ]

    MainWindow._sync_monitor_window_menu(window, windows)

    assert window._monitor_window_button.text() == "监控窗口 0/2"
    assert window._monitor_window_button.property("selectionState") == "empty"
    assert "尚未选择监控窗口" in window._monitor_window_button.toolTip()
    assert 'selectionState="empty"' in APP_QSS

    window._monitor_selected_titles = {"EVE - Pilot C"}
    MainWindow._sync_monitor_window_menu(window, [])

    assert window._monitor_window_button.text() == "监控窗口 0/0"
    assert window._monitor_window_button.property("selectionState") == "empty"
    assert window._monitor_selected_titles == {"EVE - Pilot C"}

    MainWindow._sync_monitor_window_menu(
        window,
        [
            {"hwnd": 30, "title": "EVE - Pilot C"},
            {"hwnd": 40, "title": "EVE - Pilot D"},
        ],
    )

    assert {
        key: action.isChecked()
        for key, action in window._monitor_window_actions.items()
    } == {
        "hwnd:30:eve - pilot c": True,
        "hwnd:40:eve - pilot d": False,
    }
    assert window._monitor_selected_titles == {"EVE - Pilot C"}


def test_monitor_window_action_refreshes_after_status_and_system_change():
    qt_app()
    key = "hwnd:1:eve - pilot a"
    context = {
        "window_title": "EVE - Pilot A",
        "character_name": "Pilot A",
        "system_name": "S-KSWL",
        "system_source": "chatlog",
        "runtime_status": "运行中",
        "_location_next_check": 0.0,
    }
    window = MainWindow.__new__(MainWindow)
    window._test_monitor_menu = QMenu()
    action = window._test_monitor_menu.addAction("Pilot A")
    action.setCheckable(True)
    action.setChecked(True)
    window._monitor_window_actions = {key: action}
    window._monitor_windows_by_key = {
        key: {"hwnd": 1, "title": "EVE - Pilot A"},
    }
    window._monitor_window_titles_by_key = {key: "EVE - Pilot A"}
    window._monitor_last_status_by_title = {}
    window._worker_contexts = {key: context}
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: True},
    )()
    window._refresh_window_status_table = lambda: None
    window._monitor_window_button = None

    MainWindow._update_window_status(window, context, "异常", "OCR 失败")
    assert action.text() == "Pilot A · S-KSWL · 异常"
    assert window._monitor_last_status_by_title["EVE - Pilot A"] == {
        "system_name": "S-KSWL",
        "runtime_status": "异常",
    }

    window._location_refresh_ttl = 5.0
    window._refresh_local_system_from_chatlog = lambda context=None: (
        context.update(system_name="HB-FSO") is None
    )
    assert MainWindow._refresh_intel_location(
        window,
        force=True,
        context=context,
    ) is True
    assert window._monitor_last_status_by_title["EVE - Pilot A"][
        "system_name"
    ] == "HB-FSO"
    assert action.text() == "Pilot A · HB-FSO · 异常"


def test_detect_window_handles_button_signal_and_labels_duplicate_titles():
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

    MainWindow._detect_window(window, False)

    assert window._window_combo.items == [
        ("EVE - Pilot #1 · hwnd 1 · 800x600", 1),
        ("EVE - Pilot #2 · hwnd 2 · 1000x800", 2),
    ]
    assert window._window_label.text == "窗口：未选择"


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


def test_refresh_restarts_monitor_when_window_reappears():
    qt_app()

    class FakeButton:
        def isChecked(self):
            return True

    class FakeCapturer:
        def list_eve_windows(self, _keyword):
            return [{"hwnd": 42, "title": "EVE - Pilot", "w": 1200, "h": 900}]

    class FakeSettings:
        def get_keyword(self):
            return "EVE -"

    class FakeCombo:
        def currentData(self):
            return None

    window = MainWindow.__new__(MainWindow)
    window._settings = FakeSettings()
    window._capturer = FakeCapturer()
    window._window_combo = FakeCombo()
    window._window_signature = ()
    window._workers = {}
    window._worker = None
    window._worker_contexts = {}
    window._stopping_monitor_workers = set()
    window._monitor_btn = FakeButton()
    window._monitor_reconnect_scheduled = False
    window._detect_window = lambda windows=None: None
    window._build_monitor_targets = lambda: [{"key": "window"}]
    window._log_message = lambda _message: None
    starts = []
    window._start_monitor = lambda **kwargs: starts.append(kwargs)

    MainWindow._refresh_detected_windows(window)
    MainWindow._reconnect_monitor(window)

    assert starts == [{"identity_checked": True}]


def test_refresh_restarts_monitor_after_worker_exits_with_unchanged_window_signature(
    monkeypatch,
):
    """Recover when the window returns before a missing-window refresh observes it."""
    qt_app()
    callbacks = []
    starts = []
    monitor_window = {
        "hwnd": 42,
        "title": "EVE - Pilot",
        "x": 0,
        "y": 0,
        "w": 1200,
        "h": 900,
    }
    monitor_key = "hwnd:42:eve - pilot"

    class FakeWorker:
        def __init__(self):
            self.running = True

        def isRunning(self):
            return self.running

    worker = FakeWorker()

    window = MainWindow.__new__(MainWindow)
    window._settings = type(
        "Settings",
        (),
        {"get_keyword": lambda self: "EVE -"},
    )()
    window._capturer = type(
        "Capturer",
        (),
        {"list_eve_windows": lambda self, _keyword: [dict(monitor_window)]},
    )()
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 42},
    )()
    window._window_signature = (
        (42, "EVE - Pilot", 0, 0, 1200, 900),
    )
    window._monitor_window_actions = {
        monitor_key: FakeCheckAction(True),
    }
    window._workers = {monitor_key: worker}
    window._worker = window._workers[monitor_key]
    window._worker_contexts = {}
    window._stopping_monitor_workers = set()
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: True},
    )()
    window._monitor_reconnect_scheduled = False
    window._sync_monitor_target_geometry = lambda _windows: None
    window._sync_monitor_window_status = lambda _windows: None
    window._detect_window = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("unchanged window signature unexpectedly rebuilt the selector")
    )
    window._build_monitor_targets = lambda: [{"key": monitor_key}]
    window._log_message = lambda _message: None
    window._start_monitor = lambda **kwargs: starts.append(kwargs)
    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda _delay, callback: callbacks.append(callback),
    )

    MainWindow._refresh_detected_windows(window)
    assert callbacks == []

    worker.running = False
    MainWindow._refresh_detected_windows(window)
    assert len(callbacks) == 1
    callbacks[0]()
    assert starts == [{"identity_checked": True}]


def test_refresh_rebuilds_all_targets_when_one_of_multiple_workers_exits(
    monkeypatch,
):
    """A surviving client must not prevent another selected client recovering."""
    qt_app()
    callbacks = []
    starts = []
    windows = [
        {
            "hwnd": 1,
            "title": "EVE - Pilot A",
            "x": 0,
            "y": 0,
            "w": 1200,
            "h": 900,
        },
        {
            "hwnd": 2,
            "title": "EVE - Pilot B",
            "x": 1200,
            "y": 0,
            "w": 1200,
            "h": 900,
        },
    ]
    first_key = "hwnd:1:eve - pilot a"
    second_key = "hwnd:2:eve - pilot b"

    class FakeSignal:
        def disconnect(self):
            pass

    class FakeWorker:
        def __init__(self, running):
            self.running = running
            self.stop_calls = 0
            self.wait_calls = 0
            self.ocr_snapshot = FakeSignal()
            self.hostile_detected = FakeSignal()
            self.status_update = FakeSignal()
            self.scan_complete = FakeSignal()

        def isRunning(self):
            return self.running

        def stop(self):
            self.stop_calls += 1

        def wait(self, *_args):
            self.wait_calls += 1
            raise AssertionError("monitor recovery waited on the UI thread")

    first_worker = FakeWorker(True)
    second_worker = FakeWorker(True)

    window = MainWindow.__new__(MainWindow)
    window._settings = type(
        "Settings",
        (),
        {"get_keyword": lambda self: "EVE -"},
    )()
    window._capturer = type(
        "Capturer",
        (),
        {"list_eve_windows": lambda self, _keyword: list(windows)},
    )()
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 1},
    )()
    window._window_signature = (
        (1, "EVE - Pilot A", 0, 0, 1200, 900),
        (2, "EVE - Pilot B", 1200, 0, 1200, 900),
    )
    window._monitor_window_actions = {
        first_key: FakeCheckAction(True),
        second_key: FakeCheckAction(True),
    }
    window._workers = {
        first_key: first_worker,
        second_key: second_worker,
    }
    window._worker = window._workers[first_key]
    window._worker_contexts = {}
    window._stopping_monitor_workers = set()
    window._monitor_btn = type(
        "Button",
        (),
        {
            "isChecked": lambda self: True,
            "setEnabled": lambda self, _enabled: None,
        },
    )()
    window._monitor_reconnect_scheduled = False
    window._monitor_restart_pending = False
    window._sync_monitor_target_geometry = lambda _windows: None
    window._sync_monitor_window_status = lambda _windows: None
    window._detect_window = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("unchanged window signature unexpectedly rebuilt the selector")
    )
    window._build_monitor_targets = lambda: [
        {"key": first_key},
        {"key": second_key},
    ]
    window._log_message = lambda _message: None
    window._refresh_window_status_table = lambda: None
    window._refresh_monitor_window_action_labels = lambda: None
    window._start_monitor = lambda **kwargs: starts.append(kwargs)
    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda _delay, callback: callbacks.append(callback),
    )

    MainWindow._refresh_detected_windows(window)
    assert callbacks == []

    second_worker.running = False
    MainWindow._refresh_detected_windows(window)
    assert len(callbacks) == 1
    callbacks.pop(0)()
    assert starts == []
    assert first_worker.stop_calls == 1
    assert second_worker.stop_calls == 1
    assert first_worker.wait_calls == 0
    assert second_worker.wait_calls == 0

    first_worker.running = False
    callbacks.pop(0)()
    assert starts == [{"identity_checked": True}]


def test_transient_missing_window_does_not_override_running_monitor_status():
    class FakeButton:
        def isChecked(self):
            return True

    class FakeLabel:
        def setText(self, text):
            self.text = text

        def setStyleSheet(self, style):
            self.style = style

    class FakeWorker:
        def isRunning(self):
            return True

    window = MainWindow.__new__(MainWindow)
    window._monitor_btn = FakeButton()
    window._status_label = FakeLabel()
    window._workers = {"window": FakeWorker()}
    window._worker = None

    MainWindow._sync_monitor_window_status(window, [])

    assert window._status_label.text == "监控中"
    assert "#37d6b0" in window._status_label.style


def test_missing_window_shows_waiting_after_monitor_worker_stops():
    class FakeButton:
        def isChecked(self):
            return True

    class FakeLabel:
        def setText(self, text):
            self.text = text

        def setStyleSheet(self, style):
            self.style = style

    window = MainWindow.__new__(MainWindow)
    window._monitor_btn = FakeButton()
    window._status_label = FakeLabel()
    window._workers = {}
    window._worker = None

    MainWindow._sync_monitor_window_status(window, [])

    assert window._status_label.text == "等待 EVE 窗口重新出现"
    assert "#f0b35a" in window._status_label.style


class FakeStatusTable:
    def __init__(self, columns=6):
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
            "character_name": "Pilot A",
            "system_name": "S-KSWL",
            "window_title": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
            "runtime_status": "运行中",
            "last_action": "监控线程已启动",
        },
        "second": {
            "character_name": "Pilot B",
            "system_name": "HB-FSO",
            "window_title": "EVE - Pilot B",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
            "runtime_status": "扫描中",
            "last_action": "OCR 名单 2",
        },
    }

    MainWindow._refresh_window_status_table(window)

    assert table_text(table) == [
        ["Pilot A", "S-KSWL", "EVE - Pilot A", "200x600 @ 600,0", "运行中", "监控线程已启动"],
        ["Pilot B", "HB-FSO", "EVE - Pilot B", "220x420 @ 760,190", "扫描中", "OCR 名单 2"],
    ]
    assert table.resized is False


def test_window_status_table_lists_all_detected_windows_before_monitoring():
    table = FakeStatusTable()
    window = MainWindow.__new__(MainWindow)
    window._window_status_table = table
    window._worker_contexts = {}
    window._detected_window_contexts = {
        "first": {
            "character_name": "Pilot A",
            "system_name": "S-KSWL",
            "window_title": "EVE - Pilot A",
            "region": {"x": 600, "y": 0, "w": 200, "h": 600},
            "runtime_status": "待启动",
            "last_action": "窗口已选择，监控尚未启动",
        },
        "second": {
            "character_name": "Pilot B",
            "system_name": "HB-FSO",
            "window_title": "EVE - Pilot B",
            "region": {"x": 760, "y": 190, "w": 220, "h": 420},
            "runtime_status": "未选择",
            "last_action": "未选择为监控窗口",
        },
    }

    MainWindow._refresh_window_status_table(window)

    assert table_text(table) == [
        [
            "Pilot A",
            "S-KSWL",
            "EVE - Pilot A",
            "200x600 @ 600,0",
            "待启动",
            "窗口已选择，监控尚未启动",
        ],
        [
            "Pilot B",
            "HB-FSO",
            "EVE - Pilot B",
            "220x420 @ 760,190",
            "未选择",
            "未选择为监控窗口",
        ],
    ]


def test_detected_context_keeps_idle_state_separate_from_worker_state():
    key = "hwnd:1:eve - pilot a"
    running = {
        "key": key,
        "character_name": "Pilot A",
        "system_name": "S-KSWL",
        "window_title": "EVE - Pilot A",
        "runtime_status": "运行中",
        "last_action": "扫描中",
    }
    window = MainWindow.__new__(MainWindow)
    window._worker_contexts = {key: running}
    window._detected_window_contexts = {}
    window._monitor_last_status_by_title = {}
    window._monitor_window_actions = {key: FakeCheckAction(True)}
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 1},
    )()
    window._region_prefs = type(
        "Prefs",
        (),
        {"resolve_region": lambda self, _item: None},
    )()
    window._capturer = type(
        "Capturer",
        (),
        {
            "get_member_list_region": lambda self, _item: {
                "x": 600,
                "y": 0,
                "w": 200,
                "h": 600,
            }
        },
    )()
    window._use_local_system_log = False
    window._refresh_monitor_window_action_labels = lambda: None
    window._refresh_window_status_table = lambda: None

    MainWindow._refresh_detected_window_contexts(
        window,
        [{"hwnd": 1, "title": "EVE - Pilot A"}],
    )

    detected = window._detected_window_contexts[key]
    assert detected is not running
    assert detected["system_name"] == "S-KSWL"
    assert detected["runtime_status"] == "待启动"
    assert detected["last_action"] == "窗口已选择，监控尚未启动"


def test_idle_monitor_selection_refreshes_detected_contexts_immediately():
    calls = []
    windows = [{"hwnd": 1, "title": "EVE - Pilot A"}]
    window = MainWindow.__new__(MainWindow)
    window._syncing_monitor_menu = False
    window._monitor_windows_by_key = {"first": windows[0]}
    window._monitor_btn = type(
        "Button",
        (),
        {"isChecked": lambda self: False},
    )()
    window._persist_monitor_window_selection = lambda: calls.append("persist")
    window._refresh_detected_window_contexts = (
        lambda items: calls.append(("refresh", items))
    )
    window._refresh_status_cards = lambda: calls.append("status")

    MainWindow._on_monitor_window_toggled(window)

    assert calls == ["persist", ("refresh", windows), "status"]


def test_status_cards_show_idle_monitor_selection_count():
    window = MainWindow.__new__(MainWindow)
    window._status_cards = {"enabled": object()}
    window._intel_client = object()
    window._last_heartbeat_error = ""
    window._intel_system = "Unknown"
    window._intel_system_id = None
    window._intel_system_source = "default"
    window._workers = {}
    window._worker = None
    window._worker_contexts = {}
    window._monitor_window_actions = {
        "first": FakeCheckAction(True),
        "second": FakeCheckAction(False),
    }
    window._monitor_windows_by_key = {
        "first": {"title": "EVE - Pilot A"},
        "second": {"title": "EVE - Pilot B"},
    }
    window._manual_region = None
    window._detected_region = None
    values = {}
    window._set_status_card = (
        lambda key, value, tone="idle": values.__setitem__(key, (value, tone))
    )

    MainWindow._refresh_status_cards(window)

    assert values["ocr"] == ("等待启动", "idle")
    assert values["window"] == ("已选 1/2", "ok")


def test_detected_system_for_character_reuses_local_context():
    window = MainWindow.__new__(MainWindow)
    window._detected_window_contexts = {
        "first": {
            "character_name": "Pilot A",
            "system_name": "S-KSWL",
        }
    }
    window._worker_contexts = {}

    assert MainWindow._detected_system_for_character(window, "pilot a") == "S-KSWL"
    assert MainWindow._detected_system_for_character(window, "Pilot B") == "Unknown"


def test_update_window_status_records_last_action():
    table = FakeStatusTable()
    context = {
        "character_name": "Pilot A",
        "system_name": "S-KSWL",
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
        ["Pilot A", "S-KSWL", "EVE - Pilot A", "200x600 @ 600,0", "识别到名单", "本地名单 3"]
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

    MainWindow._start_monitor(window, identity_checked=True)

    assert window._monitor_btn.checked is False
    assert messages == ["当前没有可用的 EVE 窗口。"]


def test_start_monitor_deduplicates_repeated_start_request(monkeypatch):
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

    MainWindow._start_monitor(window, identity_checked=True)
    MainWindow._start_monitor(window, identity_checked=True)

    assert len(messages) == 1


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

        def set_ocr_enabled(self, enabled):
            self.ocr_enabled = enabled

    worker = FakeWorker()
    window = MainWindow.__new__(MainWindow)
    window._settings = type(
        "Settings",
        (),
        {
            "get_interval": lambda self: 5.0,
            "get_ocr_enabled": lambda self: False,
        },
    )()
    window._running_workers = lambda: [worker]
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._refresh_status_cards = lambda: None

    MainWindow._apply_scan_settings(window)

    assert worker.interval == 5.0
    assert worker.ocr_enabled is False
    assert window._log_messages == ["扫描间隔已实时更新为 5 秒，OCR 已关闭"]


def test_hostile_count_upload_does_not_depend_on_local_alert_controller():
    qt_app()

    class UploadManager:
        def __init__(self):
            self.calls = []

        def submit_presence(self, key, payload, metadata):
            self.calls.append((key, payload, metadata))

    manager = UploadManager()
    window = MainWindow.__new__(MainWindow)
    window._intel_client = object()
    window._uploads_enabled = True
    window._upload_manager = manager
    window._alert_controller = None
    window._heartbeat_client_id = "detector:device"
    window._refresh_intel_location = lambda context: True
    context = {
        "client_id": "detector:device:pilot-a",
        "source_instance": "EVE - Pilot A",
        "window_title": "EVE - Pilot A",
        "system_name": "Tama",
        "system_id": 30002813,
    }

    MainWindow._on_hostile_icon_detected(window, 2, context)

    assert len(manager.calls) == 1
    key, payload, metadata = manager.calls[0]
    assert key == "detector:device:pilot-a"
    assert payload == {
        "client_id": "detector:device:pilot-a",
        "source_instance": "EVE - Pilot A",
        "system_name": "Tama",
        "system_id": 30002813,
        "hostile_icon_count": 2,
    }
    assert metadata["kind"] == "hostile_presence"
    assert metadata["context"] is context


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

    MainWindow._publish_heartbeat(window, monitoring_override=False)

    offline_payload = window._intel_client.payload
    assert offline_payload["status"] == "idle"
    assert offline_payload["details"]["monitoring"] is False
    assert all(
        target["monitoring"] is False
        for target in offline_payload["details"]["targets"]
    )


def test_stopped_monitor_skips_ocr_but_publishes_idle_heartbeat():
    class Client:
        def __init__(self):
            self.heartbeat = None

        def post_ocr_snapshot(self, **_payload):
            raise AssertionError("stopped monitor uploaded OCR")

        def post_heartbeat(self, **payload):
            self.heartbeat = payload
            return {"client_id": payload["client_id"], "online": True}

    class FakeCombo:
        def currentText(self):
            return "EVE - Pilot"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = Client()
    window._uploads_enabled = False
    window._workers = {}
    window._worker = None
    window._worker_contexts = {}
    window._heartbeat_client_id = "detector-client:test"
    window._heartbeat_interval = 15.0
    window._heartbeat_runtime = {
        "client_version": "test-version",
        "host": "test-host",
    }
    window._heartbeat_last_action = "monitor_stopped"
    window._heartbeat_last_error = ""
    window._heartbeat_last_success_at = ""
    window._intel_system = "Unknown"
    window._intel_system_source = "default"
    window._popup_alerts_enabled = False
    window._window_combo = FakeCombo()
    window._last_heartbeat_error = ""
    window._refresh_status_cards = lambda: None

    MainWindow._publish_ocr_snapshot(window, ["Alice"])
    MainWindow._publish_heartbeat(window)

    assert window._intel_client.heartbeat["status"] == "idle"
    assert window._intel_client.heartbeat["details"]["monitoring"] is False


def test_heartbeat_reports_multi_window_and_recovered_transport_errors():
    class Client:
        def __init__(self):
            self.payload = None

        def post_heartbeat(self, **payload):
            self.payload = payload
            return {"client_id": payload["client_id"]}

    class FakeCombo:
        def currentText(self):
            return "EVE - Pilot A"

    window = MainWindow.__new__(MainWindow)
    window._intel_client = Client()
    window._workers = {"a": object(), "b": object()}
    window._worker_contexts = {
        "a": {
            "key": "a", "client_id": "detector:a", "window_title": "EVE - Pilot A",
            "source_instance": "EVE - Pilot A", "character_name": "Pilot A",
            "system_name": "S-KSWL", "system_id": None, "system_source": "chatlog",
            "region": {}, "runtime_status": "上报异常", "last_error": "OCR timeout",
        },
        "b": {
            "key": "b", "client_id": "detector:b", "window_title": "EVE - Pilot B",
            "source_instance": "EVE - Pilot B", "character_name": "Pilot B",
            "system_name": "HB-FSO", "system_id": None, "system_source": "chatlog",
            "region": {}, "runtime_status": "运行中", "last_error": "",
        },
    }
    window._heartbeat_client_id = "detector:test"
    window._heartbeat_interval = 15.0
    window._heartbeat_runtime = {"client_version": "test", "host": "host"}
    window._heartbeat_last_action = "ocr_snapshot:1"
    window._heartbeat_last_error = ""
    window._heartbeat_last_success_at = ""
    window._last_heartbeat_error = "connection reset"
    window._intel_system = "S-KSWL"
    window._intel_system_source = "chatlog"
    window._popup_alerts_enabled = False
    window._window_combo = FakeCombo()
    window._refresh_status_cards = lambda: None

    MainWindow._publish_heartbeat(window, monitoring_override=True)

    assert window._intel_client.payload["status"] == "error"
    details = window._intel_client.payload["details"]
    assert "Pilot A: OCR timeout" in details["last_error"]
    assert "心跳连接: connection reset" in details["last_error"]
    assert details["targets"][0]["last_error"] == "OCR timeout"
    assert details["targets"][1]["runtime_status"] == "运行中"


def test_stop_monitor_keeps_connection_timer_and_publishes_idle_status():
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

    class FakeAlertController:
        def __init__(self):
            self.forgotten = []

        def forget_local_monitoring_systems(self, systems):
            self.forgotten.append(list(systems))

    window = MainWindow.__new__(MainWindow)
    window._uploads_enabled = True
    window._heartbeat_timer = FakeTimer()
    window._network_tasks = FakeNetworkTasks()
    window._workers = {"eve-hajimi6": object()}
    window._worker_contexts = {
        "eve-hajimi6": {"system_name": "S-KSWL"},
    }
    window._alert_controller = FakeAlertController()
    window._monitor_btn = FakeButton()
    window._status_label = FakeLabel()
    window._log_messages = []
    window._log_message = lambda message: window._log_messages.append(message)
    window._heartbeat_last_action = "running"
    window._heartbeat_last_success_at = "previous-success"
    window._refresh_status_cards = lambda: None
    heartbeat_calls = []
    stop_worker_timeouts = []
    window._publish_heartbeat = lambda **kwargs: heartbeat_calls.append(kwargs)
    window._stop_monitor_workers = (
        lambda timeout_ms: stop_worker_timeouts.append(timeout_ms) or True
    )

    MainWindow._stop_monitor(window)

    assert window._uploads_enabled is False
    assert window._heartbeat_timer.active is True
    assert window._heartbeat_timer.stop_calls == 0
    assert window._network_tasks.cancel_calls == 1
    assert window._alert_controller.forgotten == [["S-KSWL"]]
    assert window._monitor_btn.text == "开始监控"
    assert window._status_label.text == "已停止"
    assert window._heartbeat_last_action == "monitor_stopped"
    assert window._heartbeat_last_success_at == "previous-success"
    assert stop_worker_timeouts == [0]
    assert heartbeat_calls == [
        {
            "monitoring_override": False,
            "task_key": "heartbeat:offline",
        }
    ]
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


def test_stop_monitor_workers_returns_without_waiting_for_async_cleanup(monkeypatch):
    callbacks = []

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
            self.wait_calls = 0

        def isRunning(self):
            return self.running

        def stop(self):
            self.stop_calls += 1

        def wait(self, *_args):
            self.wait_calls += 1
            raise AssertionError("async monitor stop waited on the UI thread")

    class FakeButton:
        def __init__(self):
            self.enabled = []

        def setEnabled(self, enabled):
            self.enabled.append(enabled)

    worker = FakeWorker()
    window = MainWindow.__new__(MainWindow)
    window._workers = {"first": worker}
    window._worker = worker
    window._worker_contexts = {"first": {"window_title": "A"}}
    window._stopping_monitor_workers = set()
    window._monitor_btn = FakeButton()
    window._log_message = lambda _message: None
    window._refresh_window_status_table = lambda: None
    monkeypatch.setattr(
        "app.ui.main_window.QTimer.singleShot",
        lambda delay, callback: callbacks.append((delay, callback)),
    )

    assert MainWindow._stop_monitor_workers(window, timeout_ms=0) is True

    assert worker.stop_calls == 1
    assert worker.wait_calls == 0
    assert window._workers == {}
    assert window._worker_contexts == {}
    assert window._worker is None
    assert window._stopping_monitor_workers == {worker}
    assert window._monitor_btn.enabled == [False]
    assert callbacks[0][0] == 50

    worker.running = False
    callbacks.pop(0)[1]()

    assert window._stopping_monitor_workers == set()
    assert window._monitor_btn.enabled == [False, True]


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


def test_preview_capture_worker_returns_detached_background_capture(monkeypatch):
    qt_app()
    calls = []
    expected_image_data = b"preview-png"
    captured = []
    failed = []

    class FakeImage:
        def save(self, buffer, format):
            calls.append(("save", format))
            buffer.write(expected_image_data)

    class FakeCapturer:
        def __init__(self):
            calls.append(("create",))

        def select_window(self, hwnd, title, width, height):
            calls.append(("select", hwnd, title, width, height))

        def screenshot(self, x, y, width, height):
            calls.append(("screenshot", x, y, width, height))
            return FakeImage()

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("app.ui.main_window.Capturer", FakeCapturer)
    worker = PreviewCaptureWorker(
        {
            "hwnd": 99,
            "title": "EVE - Selected",
            "w": 1920,
            "h": 1000,
        },
        {"x": 362, "y": 145, "w": 197, "h": 833},
    )
    worker.captured.connect(
        lambda image: (calls.append(("emit",)), captured.append(image))
    )
    worker.failed.connect(failed.append)

    worker.run()

    assert captured == [expected_image_data]
    assert failed == []
    assert calls == [
        ("create",),
        ("select", 99, "EVE - Selected", 1920, 1000),
        ("screenshot", 362, 145, 197, 833),
        ("save", "PNG"),
        ("close",),
        ("emit",),
    ]


def test_preview_does_not_hide_client_or_activate_game_window():
    region = {"x": 362, "y": 145, "w": 197, "h": 833}
    selected_window = {
        "hwnd": 99,
        "title": "EVE - Selected",
        "w": 1920,
        "h": 1000,
    }
    window = MainWindow.__new__(MainWindow)
    window._manual_region = region
    window._detected_region = None
    window._preview_capture_worker = None
    window._current_window_info = lambda: selected_window
    window.hide = lambda: (_ for _ in ()).throw(AssertionError("client hidden"))
    window._capturer = type(
        "Capturer",
        (),
        {
            "activate_window": lambda self, hwnd: (_ for _ in ()).throw(
                AssertionError(f"game activated: {hwnd}")
            )
        },
    )()
    starts = []
    window._start_preview_capture = (
        lambda target, capture_region: starts.append((target, capture_region))
    )

    MainWindow._preview_region(window)

    assert starts == [(selected_window, region)]


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


def test_select_region_maps_physical_window_to_qt_logical_geometry(monkeypatch):
    created = {}

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeSelector:
        def __init__(
            self,
            x,
            y,
            w,
            h,
            title="",
            *,
            physical_geometry=None,
        ):
            created.update(
                geometry={"x": x, "y": y, "w": w, "h": h},
                title=title,
                physical_geometry=physical_geometry,
            )
            self.region_selected = FakeSignal()
            self.selector_closed = FakeSignal()

        def show(self):
            created["shown"] = True

    class FakeGeometry:
        def x(self):
            return 1920

        def y(self):
            return 0

        def width(self):
            return 2560

        def height(self):
            return 1440

    class FakeScreen:
        def geometry(self):
            return FakeGeometry()

    class FakeCapturer:
        def activate_window(self, _hwnd):
            pass

        def get_window_info(self, _hwnd):
            return {
                "hwnd": 99,
                "title": "EVE - Pilot",
                "x": 2304,
                "y": 216,
                "w": 1920,
                "h": 1080,
            }

        def get_monitor_geometry(self, _hwnd):
            return {
                "x": 1920,
                "y": 0,
                "w": 3840,
                "h": 2160,
                "primary": False,
            }

        def select_window(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr("app.ui.main_window.RegionSelector", FakeSelector)
    window = MainWindow.__new__(MainWindow)
    window._window_combo = type(
        "Combo",
        (),
        {"currentData": lambda self: 99},
    )()
    window._capturer = FakeCapturer()
    window._settings = type("Settings", (), {"get_keyword": lambda self: "EVE -"})()
    window._current_window_info = lambda: window._capturer.get_window_info(99)
    window._screen_for_monitor_geometry = lambda _geometry: FakeScreen()
    window._log_message = lambda _message: None
    window.hide = lambda: None
    window._on_region_selected = lambda *_args: None
    window._on_selector_closed = lambda *_args: None

    MainWindow._select_region(window)

    assert created == {
        "geometry": {"x": 2176, "y": 144, "w": 1280, "h": 720},
        "title": "EVE - Pilot",
        "physical_geometry": {
            "x": 2304,
            "y": 216,
            "w": 1920,
            "h": 1080,
        },
        "shown": True,
    }


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
