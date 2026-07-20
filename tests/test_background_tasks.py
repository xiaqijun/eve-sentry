import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.ui.background_tasks import BackgroundTaskRunner


def test_background_task_runner_coalesces_repeated_jobs_to_the_latest():
    app = QApplication.instance() or QApplication([])
    gate = threading.Event()
    calls = []
    completed = []
    runner = BackgroundTaskRunner(max_workers=1)

    def first():
        gate.wait(timeout=2)
        calls.append("first")
        return 1

    def second():
        calls.append("second")
        return 2

    def on_completed(key, future, context):
        completed.append((key, future.result(), context))
        runner.finish(key)

    runner.completed.connect(on_completed)
    runner.submit_latest("ocr:pilot", first, "old")
    runner.submit_latest("ocr:pilot", second, "latest")
    gate.set()

    deadline = time.monotonic() + 3
    while len(completed) < 2 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    runner.shutdown()

    assert calls == ["first", "second"]
    assert completed == [
        ("ocr:pilot", 1, "old"),
        ("ocr:pilot", 2, "latest"),
    ]


def test_background_task_runner_can_discard_coalesced_replacement():
    app = QApplication.instance() or QApplication([])
    gate = threading.Event()
    calls = []
    completed = []
    runner = BackgroundTaskRunner(max_workers=1)

    def first():
        gate.wait(timeout=2)
        calls.append("first")
        return 1

    def second():
        calls.append("second")
        return 2

    def on_completed(key, future, context):
        completed.append((key, future.result(), context))
        runner.finish(key)

    runner.completed.connect(on_completed)
    runner.submit_latest("ocr:pilot", first, "running")
    runner.submit_latest("ocr:pilot", second, "queued")
    runner.cancel_latest()
    gate.set()

    deadline = time.monotonic() + 3
    while len(completed) < 1 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    runner.shutdown()

    assert calls == ["first"]
    assert completed == [("ocr:pilot", 1, "running")]
