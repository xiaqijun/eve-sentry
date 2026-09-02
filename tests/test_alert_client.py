from argparse import Namespace

from app.alert_client import (
    AlertClientState,
    AlertEventWorker,
    AlertTrayController,
    is_local_monitored_account,
    local_accounts_from_windows,
    merge_map_accounts,
    monitored_accounts_from_bootstrap,
)


def test_monitored_accounts_from_bootstrap_keeps_online_monitoring_targets():
    accounts = monitored_accounts_from_bootstrap(
        {
            "clients": {
                "heartbeats": [
                    {
                        "client_id": "detector-client:one",
                        "client_type": "detector_client",
                        "online": True,
                        "details": {
                            "monitoring": True,
                            "targets": [
                                {
                                    "client_id": "detector-client:one:alice",
                                    "character_name": "Alice",
                                    "source_instance": "EVE - Alice",
                                    "system_name": "Tama",
                                    "system_id": 30002813,
                                    "monitoring": True,
                                },
                                {
                                    "client_id": "detector-client:one:bob",
                                    "character_name": "Bob",
                                    "system_name": "Kedama",
                                    "monitoring": False,
                                },
                            ],
                        },
                    },
                    {
                        "client_id": "detector-client:offline",
                        "client_type": "detector_client",
                        "online": False,
                        "details": {"monitoring": True, "system": "Jita"},
                    },
                ]
            }
        }
    )

    assert accounts == [
        {
            "key": "detector-client:one:alice|alice|eve - alice",
            "label": "Alice",
            "character_name": "Alice",
            "client_id": "detector-client:one:alice",
            "system_name": "Tama",
            "system_id": 30002813,
            "monitoring": True,
        }
    ]


def test_monitored_accounts_from_bootstrap_omits_capture_offline_targets():
    accounts = monitored_accounts_from_bootstrap(
        {
            "clients": {
                "heartbeats": [
                    {
                        "client_type": "detector_client",
                        "online": True,
                        "details": {
                            "monitoring": True,
                            "targets": [
                                {
                                    "client_id": "window:offline",
                                    "character_name": "Alice",
                                    "system_name": "Tama",
                                    "monitoring": True,
                                    "capture_online": False,
                                }
                            ],
                        },
                    }
                ]
            }
        }
    )

    assert accounts == []


def test_local_accounts_from_windows_uses_stable_character_identity():
    accounts = local_accounts_from_windows(
        [
            {"hwnd": 11, "title": "EVE - Alice"},
            {"hwnd": 22, "title": "EVE - Bob"},
            {"hwnd": 33, "title": "EVE - Alice"},
            {"hwnd": 44, "title": ""},
        ],
        {"alice": "Tama", "bob": "Kedama"},
    )

    assert [item["character_name"] for item in accounts] == ["Alice", "Bob"]
    assert [item["key"] for item in accounts] == [
        "local-account:alice",
        "local-account:bob",
    ]
    assert [item["system_name"] for item in accounts] == [
        "Tama",
        "Kedama",
    ]


def test_merge_map_accounts_enriches_local_window_without_duplicate():
    local = local_accounts_from_windows(
        [
            {"hwnd": 11, "title": "EVE - Alice"},
            {"hwnd": 12, "title": "EVE - Alice"},
        ],
    )
    remote = [
        {
            "key": "remote-alice",
            "label": "Alice",
            "character_name": "Alice",
            "client_id": "detector-client:one:alice",
            "system_name": "Tama",
            "system_id": 30002813,
        },
        {
            "key": "remote-bob",
            "label": "Bob",
            "character_name": "Bob",
            "system_name": "Kedama",
            "system_id": 30002814,
        },
        {
            "key": "stale-local-charlie",
            "label": "Charlie",
            "character_name": "Charlie",
            "system_name": "Jita",
            "local": True,
        },
    ]

    merged = merge_map_accounts(local, remote)

    assert len(merged) == 2
    assert merged[0]["key"] == "local-account:alice"
    assert merged[0]["system_name"] == "Tama"
    assert merged[0]["system_id"] == 30002813
    assert merged[1]["character_name"] == "Bob"


def test_merge_map_accounts_does_not_mark_unmonitored_local_window_online():
    local = local_accounts_from_windows(
        [{"hwnd": 11, "title": "EVE - Alice"}],
        {"alice": "Tama"},
    )

    merged = merge_map_accounts(local, [])

    assert merged[0]["system_name"] == "Tama"
    assert merged[0]["monitoring"] is False


def test_sync_map_accounts_refreshes_when_monitoring_stops():
    class FakeOverlay:
        def __init__(self):
            self.calls = []

        def set_map_accounts(self, accounts):
            self.calls.append(accounts)

    account = {
        "key": "local-account:alice",
        "character_name": "Alice",
        "system_name": "Tama",
        "system_id": None,
        "local": True,
        "monitoring": True,
    }
    controller = AlertTrayController.__new__(AlertTrayController)
    controller.overlay = FakeOverlay()
    controller._map_accounts = [dict(account)]
    controller._local_map_accounts = [{**account, "monitoring": False}]
    controller._remote_map_accounts = []
    controller._map_request_signature = (("Tama",), (), 3)
    controller._refresh_local_map = lambda: None

    controller._sync_map_accounts()

    assert controller._map_accounts[0]["monitoring"] is False
    assert controller.overlay.calls == [controller._map_accounts]


def test_local_monitored_account_matches_shared_installation_identity():
    alert_client_id = "alert-client:abc123"

    assert is_local_monitored_account(
        "detector-client:abc123:alice",
        alert_client_id,
    )
    assert is_local_monitored_account("detector-client:abc123", alert_client_id)
    assert not is_local_monitored_account(
        "detector-client:other:bob",
        alert_client_id,
    )
    assert not is_local_monitored_account("", alert_client_id)


def test_controller_persists_explicit_map_account_selection(tmp_path):
    class Overlay:
        @staticmethod
        def map_selection_memory():
            return ["local-account:alice"]

    state_path = tmp_path / "alert_state.json"
    state = AlertClientState(state_path)
    state.load_seen_ids()
    controller = AlertTrayController.__new__(AlertTrayController)
    controller.state = state
    controller.overlay = Overlay()
    refreshes = []
    controller._refresh_local_map = lambda **kwargs: refreshes.append(kwargs)

    controller._on_map_options_changed(["local-account:alice"], 3)

    reloaded = AlertClientState(state_path)
    reloaded.load_seen_ids()
    assert reloaded.map_selected_account_keys() == ["local-account:alice"]
    assert refreshes == [{"selected_keys": ["local-account:alice"], "hops": 3}]


def test_alert_worker_connects_sse_before_posting_heartbeat(tmp_path):
    calls = []
    worker = None

    class FakeApi:
        def __init__(self, server, timeout, api_key):
            calls.append(("init", server, timeout, api_key))

        def iter_events(self, **kwargs):
            calls.append(("events", kwargs))
            yield {"event": "bootstrap", "data": {"active_intel": []}}
            worker._stop_requested = True

        def post_heartbeat(self, **kwargs):
            calls.append(("heartbeat", kwargs))
            return {"client_id": kwargs["client_id"]}

    worker = AlertEventWorker(
        "http://intel.example",
        AlertClientState(tmp_path / "alerts.json"),
        timeout=5.0,
        api_key="eve_valid",
        api_factory=FakeApi,
    )
    statuses = []
    bootstraps = []
    worker.status_changed.connect(lambda status, message: statuses.append((status, message)))
    worker.bootstrap_received.connect(bootstraps.append)

    worker.run()

    assert calls[1][0] == "events"
    assert calls[1][1]["include_bootstrap"] is True
    assert calls[2][0] == "heartbeat"
    assert statuses == [("connected", "")]
    assert bootstraps == [{"active_intel": []}]


def test_alert_worker_keeps_online_status_during_normal_stream_rollover(tmp_path):
    worker = None
    stream_count = 0

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def iter_events(self, **_kwargs):
            nonlocal stream_count
            stream_count += 1
            if stream_count == 1:
                yield {"event": "bootstrap", "data": {"active_intel": []}}
                return
            worker._stop_requested = True
            return
            yield

        def post_heartbeat(self, **kwargs):
            return {"client_id": kwargs["client_id"]}

    worker = AlertEventWorker(
        "http://intel.example",
        AlertClientState(tmp_path / "alerts.json"),
        timeout=5.0,
        api_factory=FakeApi,
    )
    statuses = []
    worker.status_changed.connect(lambda status, message: statuses.append((status, message)))

    worker.run()

    assert stream_count == 2
    assert statuses == [("connected", "")]


def test_alert_controller_reconnect_preserves_api_key(monkeypatch, tmp_path):
    created = []

    class FakeSignal:
        def connect(self, callback):
            pass

    class FakeWorker:
        def __init__(self, server, state, **kwargs):
            created.append((server, state, kwargs))
            self.alert_received = FakeSignal()
            self.safe_received = FakeSignal()
            self.bootstrap_received = FakeSignal()
            self.status_changed = FakeSignal()

        def isRunning(self):
            return False

        def start(self):
            pass

    monkeypatch.setattr("app.alert_client.AlertEventWorker", FakeWorker)
    controller = AlertTrayController.__new__(AlertTrayController)
    controller.args = Namespace(
        server="http://intel.example",
        timeout=30.0,
        heartbeat_interval=10.0,
        reconnect_max_delay=30.0,
        api_key="eve_secret",
    )
    controller.state = AlertClientState(tmp_path / "alerts.json")
    controller.api_factory = object()
    controller._worker = FakeWorker("old", controller.state)

    controller._restart_worker()

    assert created[-1][2]["api_key"] == "eve_secret"


def test_alert_controller_reconnect_does_not_wait_for_running_worker(
    monkeypatch,
    tmp_path,
):
    callbacks = []
    waits = []

    class FakeSignal:
        def connect(self, callback):
            callbacks.append(callback)

    class FakeWorker:
        def __init__(self, server, state, **kwargs):
            self.alert_received = FakeSignal()
            self.safe_received = FakeSignal()
            self.bootstrap_received = FakeSignal()
            self.status_changed = FakeSignal()
            self.finished = FakeSignal()
            self.running = server == "old"

        def isRunning(self):
            return self.running

        def stop(self):
            waits.append("stop")

        def wait(self, *_args):
            waits.append("wait")

        def start(self):
            waits.append("start")

    monkeypatch.setattr("app.alert_client.AlertEventWorker", FakeWorker)
    controller = AlertTrayController.__new__(AlertTrayController)
    controller.args = Namespace(
        server="http://intel.example",
        timeout=30.0,
        heartbeat_interval=10.0,
        reconnect_max_delay=30.0,
        api_key="eve_secret",
    )
    controller.state = AlertClientState(tmp_path / "alerts.json")
    controller.api_factory = object()
    controller.overlay = type("Overlay", (), {"set_status": lambda *_args: None})()
    controller._stopped = False
    controller._worker = FakeWorker("old", controller.state)

    controller._restart_worker()

    assert waits == ["stop"]
    assert callbacks
    callbacks[-1]()
    assert waits == ["stop", "start"]
    assert not controller._worker_restart_pending


def test_alert_controller_stop_can_skip_worker_wait():
    calls = []

    class FakeWorker:
        def stop(self):
            calls.append("worker_stop")

        def isRunning(self):
            calls.append("worker_running")
            return True

        def wait(self, timeout):
            calls.append(("worker_wait", timeout))

    class FakeOverlay:
        def hide(self):
            calls.append("overlay_hide")

    controller = AlertTrayController.__new__(AlertTrayController)
    controller.args = Namespace(timeout=30.0)
    controller._worker = FakeWorker()
    controller.overlay = FakeOverlay()
    controller._tray = None

    controller.stop(wait_for_worker=False)

    assert calls == ["worker_stop", "overlay_hide"]


def test_alert_controller_stop_waits_during_application_shutdown():
    calls = []

    class FakeWorker:
        def stop(self):
            calls.append("worker_stop")

        def isRunning(self):
            return True

        def wait(self, timeout):
            calls.append(("worker_wait", timeout))

    class FakeOverlay:
        def hide(self):
            calls.append("overlay_hide")

    controller = AlertTrayController.__new__(AlertTrayController)
    controller.args = Namespace(timeout=30.0)
    controller._worker = FakeWorker()
    controller.overlay = FakeOverlay()
    controller._tray = None

    controller.stop(wait_for_worker=True)

    assert calls == [
        "worker_stop",
        "overlay_hide",
        ("worker_wait", 34000),
    ]


def test_unknown_local_account_does_not_request_empty_map():
    class FakeOverlay:
        def __init__(self):
            self.payload = None
            self.message = ""

        def set_map_payload(self, payload):
            self.payload = payload

        def set_map_message(self, message):
            self.message = message

    controller = AlertTrayController.__new__(AlertTrayController)
    controller.overlay = FakeOverlay()
    controller._map_accounts = [
        {
            "key": "local-account:alice",
            "character_name": "Alice",
            "system_name": "Unknown",
            "system_id": None,
        }
    ]
    controller._map_request_signature = ("old",)
    controller._map_worker = None

    controller._refresh_local_map(
        selected_keys=["local-account:alice"],
        hops=3,
    )

    assert controller.overlay.payload == {
        "systems": [],
        "links": [],
        "centers": [],
    }
    assert controller.overlay.message == "所选账号尚未识别当前星系"
    assert controller._map_request_signature is None


def test_stale_map_result_is_ignored_after_selection_changes():
    class FakeOverlay:
        def __init__(self):
            self.payloads = []

        def set_map_payload(self, payload):
            self.payloads.append(payload)

    controller = AlertTrayController.__new__(AlertTrayController)
    controller.overlay = FakeOverlay()
    controller._stopped = False
    controller._active_map_request_signature = (("Tama",), (), 3)
    controller._map_request_signature = None

    controller._on_map_received({"systems": [{"name": "Tama"}]})

    assert controller.overlay.payloads == []


def test_alert_controller_is_running_while_map_worker_unwinds():
    class FakeWorker:
        def __init__(self, running):
            self.running = running

        def isRunning(self):
            return self.running

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._worker = FakeWorker(False)
    controller._map_worker = FakeWorker(True)

    assert controller.is_running() is True


def test_unchanged_account_snapshot_does_not_rebuild_menu():
    class FakeOverlay:
        def __init__(self):
            self.calls = []

        def set_map_accounts(self, accounts):
            self.calls.append(accounts)

    account = {
        "key": "local-account:alice",
        "character_name": "Alice",
        "system_name": "Tama",
        "system_id": None,
        "local": True,
    }
    controller = AlertTrayController.__new__(AlertTrayController)
    controller.overlay = FakeOverlay()
    controller._map_accounts = [dict(account)]
    controller._local_map_accounts = [dict(account)]
    controller._remote_map_accounts = []
    controller._map_request_signature = (("Tama",), (), 3)

    controller._sync_map_accounts()

    assert controller.overlay.calls == []


def test_alert_sound_sequence_uses_configured_interval_and_count(monkeypatch):
    calls = []

    class FakeTimer:
        def stop(self):
            calls.append("timer_stop")

        def start(self, interval):
            calls.append(("timer_start", interval))

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._alert_muted = False
    controller._alert_volume = 0.75
    controller._alert_repeat_count = 3
    controller._alert_repeat_interval_ms = 1500
    controller._alert_sound_path = "C:/Sounds/custom.wav"
    controller._sound_repeat_timer = FakeTimer()
    monkeypatch.setattr(
        "app.alert_client.play_alert_sound",
        lambda volume, sound_path: calls.append(("sound", volume, sound_path)),
    )

    controller._play_alert_sound_sequence()
    controller._play_next_alert_sound()
    controller._play_next_alert_sound()

    assert calls == [
        "timer_stop",
        ("sound", 0.75, "C:/Sounds/custom.wav"),
        ("timer_start", 1500),
        ("sound", 0.75, "C:/Sounds/custom.wav"),
        ("timer_start", 1500),
        ("sound", 0.75, "C:/Sounds/custom.wav"),
    ]


def test_continuous_alert_sound_uses_looping_effect(monkeypatch, tmp_path):
    class FakeSound:
        def __init__(self):
            self.calls = []

        def setSource(self, source):
            self.calls.append(("source", source))

        def setVolume(self, volume):
            self.calls.append(("volume", volume))

        def setLoopCount(self, count):
            self.calls.append(("loop_count", count))

        def play(self):
            self.calls.append("play")

        def stop(self):
            self.calls.append("stop")

    sounds = []

    def factory():
        sound = FakeSound()
        sounds.append(sound)
        return sound

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._alert_volume = 0.6
    custom_path = tmp_path / "custom.wav"
    custom_path.write_bytes(b"RIFF")
    controller._alert_sound_path = str(custom_path)
    controller._continuous_sound = None
    monkeypatch.setattr("app.alert_client.QSoundEffect", factory)

    controller._start_continuous_alert_sound()

    assert len(sounds) == 1
    assert sounds[0].calls[0][0] == "source"
    assert sounds[0].calls[0][1].toLocalFile().replace("/", "\\") == str(
        custom_path.resolve()
    )
    assert ("volume", 0.6) in sounds[0].calls
    assert ("loop_count", -2) in sounds[0].calls
    assert sounds[0].calls[-1] == "play"
    controller._stop_continuous_alert_sound()
    assert sounds[0].calls[-1] == "stop"


def test_alert_controller_uses_compact_hostile_and_safe_messages(monkeypatch):
    notifications = []

    class FakeOverlay:
        def __init__(self):
            self.summaries = []
            self.history = []
            self.statuses = []

        def show_summaries(self, summaries):
            self.summaries = [dict(item) for item in summaries]
            self.history.append(self.summaries)

        def set_status(self, text, tone):
            self.statuses.append((text, tone))

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = []
    controller.overlay = FakeOverlay()
    controller._notification_callback = (
        lambda title, message: notifications.append((title, message))
    )
    controller._tray = None
    monkeypatch.setattr("app.alert_client.play_alert_sound", lambda: None)

    controller._on_alert(
        {
            "id": "evt-1",
            "system_name": "S-KSWL",
            "names": ["Alice", "Bob"],
            "created_at": "2026-07-23T14:00:00+00:00",
        }
    )
    controller._on_safe(
        {
            "system_name": "S-KSWL",
            "hostile_count": 0,
            "message": "✅ S-KSWL 清空",
        }
    )
    assert controller._recent_summaries[0]["hostile_count"] == 0
    assert controller._recent_summaries[0]["active_hostile_count"] == 0
    assert controller._recent_summaries[0]["active"] is False
    assert controller.overlay.history[-1][0]["system_name"] == "S-KSWL"
    controller._on_alert(
        {
            "id": "evt-2",
            "system_name": "S-KSWL",
            "hostile_count": 1,
            "created_at": "2026-07-23T14:01:00+00:00",
        }
    )

    assert notifications == [
        ("敌对告警", "❗ S-KSWL 来敌 2 人"),
        ("星系安全", "✅ S-KSWL 清空"),
        ("敌对告警", "❗ S-KSWL 来敌 1 人"),
    ]
    assert controller._recent_summaries == [
        {
            "id": "evt-2",
            "system_name": "S-KSWL",
            "hostile_count": 1,
            "created_at": "2026-07-23T14:01:00+00:00",
            "source_observation_id": "",
            "active_intel_id": "",
            "active": True,
            "active_hostile_count": 1,
        }
    ]


def test_alert_controller_updates_case_variant_system_as_one_tile(monkeypatch):
    class FakeOverlay:
        def __init__(self):
            self.summaries = []

        def show_summaries(self, summaries):
            self.summaries = [dict(item) for item in summaries]

        def set_status(self, *_args):
            pass

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = [
        {
            "system_name": "s-kswl",
            "hostile_count": 0,
            "active_hostile_count": 0,
            "active": False,
        }
    ]
    controller._local_hostile_counts = {}
    controller._last_notified = {}
    controller._alert_cooldown = 0
    controller._alert_muted = True
    controller.overlay = FakeOverlay()
    controller._notification_callback = lambda *_args: None
    controller._tray = None
    monkeypatch.setattr("app.alert_client.play_alert_sound", lambda: None)

    controller._on_alert(
        {"id": "evt-1", "system_name": "S-KSWL", "hostile_count": 2}
    )

    assert len(controller._recent_summaries) == 1
    assert controller.overlay.summaries[0]["hostile_count"] == 2
    assert controller.overlay.summaries[0]["active_hostile_count"] == 2
    assert controller.overlay.summaries[0]["active"] is True

    controller._on_safe({"system_name": "s-KsWl", "hostile_count": 0})

    assert len(controller._recent_summaries) == 1
    assert controller.overlay.summaries[0]["active_hostile_count"] == 0
    assert controller.overlay.summaries[0]["active"] is False


def test_alert_controller_draws_existing_monitoring_systems_without_notification():
    notifications = []

    class FakeOverlay:
        def __init__(self):
            self.summaries = []

        def show_summaries(self, summaries):
            self.summaries = [dict(item) for item in summaries]

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = []
    controller.overlay = FakeOverlay()
    controller._notification_callback = lambda *args: notifications.append(args)
    controller._tray = None

    controller.show_monitoring_systems(["S-KSWL", "S-KSWL", "Unknown"])

    assert controller.overlay.summaries == [
        {
            "system_name": "S-KSWL",
            "hostile_count": 0,
            "active_hostile_count": 0,
            "created_at": "",
            "active": False,
        }
    ]
    assert notifications == []


def test_alert_controller_forgets_stopped_local_monitoring_system_without_notice():
    notifications = []

    class FakeOverlay:
        def __init__(self):
            self.summaries = []

        def show_summaries(self, summaries):
            self.summaries = [dict(item) for item in summaries]

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = [
        {
            "system_name": "S-KSWL",
            "hostile_count": 2,
            "active_hostile_count": 2,
            "active": True,
        },
        {
            "system_name": "8-4GQM",
            "hostile_count": 0,
            "active_hostile_count": 0,
            "active": False,
        },
    ]
    controller._local_hostile_counts = {"s-kswl": ("S-KSWL", 2)}
    controller.overlay = FakeOverlay()
    controller._notification_callback = lambda *args: notifications.append(args)

    controller.forget_local_monitoring_systems(["S-KSWL"])

    assert controller._local_hostile_counts == {}
    assert controller.overlay.summaries == [
        {
            "system_name": "8-4GQM",
            "hostile_count": 0,
            "active_hostile_count": 0,
            "active": False,
        }
    ]
    assert notifications == []


def test_local_visual_count_overrides_lower_server_bootstrap(monkeypatch):
    class FakeOverlay:
        def __init__(self):
            self.summaries = []

        def show_summaries(self, summaries):
            self.summaries = [dict(item) for item in summaries]

        def set_status(self, *_args):
            pass

    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = []
    controller._local_hostile_counts = {}
    controller.overlay = FakeOverlay()
    controller._notification_callback = None
    controller._tray = None
    monkeypatch.setattr("app.alert_client.play_alert_sound", lambda: None)

    controller.update_local_hostile_count("S-KSWL", 2)
    controller._on_bootstrap(
        {
            "map": {
                "systems": [
                    {"name": "S-KSWL", "hostile_count": 1},
                ]
            },
            "active_intel": [],
        }
    )

    assert controller.overlay.summaries[0]["hostile_count"] == 2
    assert controller.overlay.summaries[0]["active_hostile_count"] == 2
    assert controller.overlay.summaries[0]["active"] is True

    controller._on_safe({"system_name": "S-KSWL", "hostile_count": 0})
    assert controller.overlay.summaries[0]["active_hostile_count"] == 2

    controller.update_local_hostile_count("S-KSWL", 0)
    assert controller.overlay.summaries[0]["hostile_count"] == 0
    assert controller.overlay.summaries[0]["active_hostile_count"] == 0
    assert controller.overlay.summaries[0]["active"] is False
