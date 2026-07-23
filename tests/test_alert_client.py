from argparse import Namespace

from app.alert_client import AlertTrayController


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

    class FakeTimer:
        def stop(self):
            calls.append("timer_stop")

    class FakeOverlay:
        def hide(self):
            calls.append("overlay_hide")

    controller = AlertTrayController.__new__(AlertTrayController)
    controller.args = Namespace(timeout=30.0)
    controller._worker = FakeWorker()
    controller._inactive_cleanup_timer = FakeTimer()
    controller.overlay = FakeOverlay()
    controller._tray = None

    controller.stop(wait_for_worker=False)

    assert calls == ["worker_stop", "timer_stop", "overlay_hide"]


def test_alert_controller_stop_waits_during_application_shutdown():
    calls = []

    class FakeWorker:
        def stop(self):
            calls.append("worker_stop")

        def isRunning(self):
            return True

        def wait(self, timeout):
            calls.append(("worker_wait", timeout))

    class FakeTimer:
        def stop(self):
            calls.append("timer_stop")

    class FakeOverlay:
        def hide(self):
            calls.append("overlay_hide")

    controller = AlertTrayController.__new__(AlertTrayController)
    controller.args = Namespace(timeout=30.0)
    controller._worker = FakeWorker()
    controller._inactive_cleanup_timer = FakeTimer()
    controller.overlay = FakeOverlay()
    controller._tray = None

    controller.stop(wait_for_worker=True)

    assert calls == [
        "worker_stop",
        "timer_stop",
        "overlay_hide",
        ("worker_wait", 34000),
    ]


def test_alert_controller_uses_compact_hostile_and_safe_messages(monkeypatch):
    notifications = []

    class FakeOverlay:
        def __init__(self):
            self.summaries = []
            self.statuses = []

        def show_summaries(self, summaries):
            self.summaries = [dict(item) for item in summaries]

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
