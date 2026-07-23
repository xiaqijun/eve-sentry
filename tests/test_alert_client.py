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
