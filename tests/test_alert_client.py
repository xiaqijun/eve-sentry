from argparse import Namespace

from app.alert_client import AlertClientState, AlertEventWorker, AlertTrayController


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
        ("敌对告警", "❗ S-KSWL 来敌"),
        ("星系安全", "✅ S-KSWL 清空"),
        ("敌对告警", "❗ S-KSWL 来敌"),
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
