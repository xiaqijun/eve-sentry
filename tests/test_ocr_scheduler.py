import threading
import time

from PIL import Image

from app.engine.ocr_scheduler import OCRRequestSuperseded, SharedOCRScheduler


def test_shared_ocr_scheduler_reuses_one_lazy_model():
    created = []

    class FakeEngine:
        def initialize(self):
            created.append(threading.get_ident())

        def recognize(self, _image, progress=None):
            _ = progress
            return [("Enemy Pilot", 0.99)]

    scheduler = SharedOCRScheduler(max_instances=1, engine_factory=FakeEngine)
    image = Image.new("RGB", (20, 20))
    try:
        assert scheduler.recognize(image) == [("Enemy Pilot", 0.99)]
        assert scheduler.recognize(image) == [("Enemy Pilot", 0.99)]
        assert len(created) == 1
        assert scheduler.health()["completed"] == 2
    finally:
        scheduler.close(wait=True)


def test_shared_ocr_scheduler_preserves_ocr_text_boxes():
    class FakeEngine:
        def initialize(self):
            pass

        def recognize_with_boxes(self, _image, progress=None):
            _ = progress
            return [("Enemy Pilot", 0.99, (3, 4, 50, 15))]

    scheduler = SharedOCRScheduler(max_instances=1, engine_factory=FakeEngine)
    try:
        result = scheduler.recognize_with_boxes_latest(
            Image.new("RGB", (60, 20)),
            request_key="window:1",
        )
        assert result == [("Enemy Pilot", 0.99, (3, 4, 50, 15))]
        assert scheduler.health()["completed"] == 1
    finally:
        scheduler.close(wait=True)


def test_shared_ocr_scheduler_bounds_parallel_model_instances():
    initialized = []
    release = threading.Event()

    class FakeEngine:
        def initialize(self):
            initialized.append(threading.get_ident())

        def recognize(self, _image, progress=None):
            _ = progress
            release.wait(1)
            return []

    scheduler = SharedOCRScheduler(max_instances=2, engine_factory=FakeEngine)
    image = Image.new("RGB", (20, 20))
    callers = [
        threading.Thread(target=scheduler.recognize, args=(image,))
        for _ in range(4)
    ]
    try:
        for caller in callers:
            caller.start()
        deadline = time.monotonic() + 1
        while len(initialized) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(set(initialized)) == 2
    finally:
        release.set()
        for caller in callers:
            caller.join(1)
        scheduler.close(wait=True)


def test_shared_ocr_scheduler_keeps_only_latest_frame_per_window():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class FakeEngine:
        def initialize(self):
            pass

        def recognize(self, _image, progress=None):
            _ = progress
            calls.append(len(calls))
            if len(calls) == 1:
                started.set()
                release.wait(1)
            return [(f"frame-{len(calls)}", 0.99)]

    scheduler = SharedOCRScheduler(max_instances=1, engine_factory=FakeEngine)
    image = Image.new("RGB", (20, 20))
    first_error = []
    second_error = []

    first = threading.Thread(
        target=lambda: _capture_error(
            first_error,
            scheduler.recognize_latest,
            image,
            request_key="window:1",
        )
    )
    second = threading.Thread(
        target=lambda: _capture_error(
            second_error,
            scheduler.recognize_latest,
            image,
            request_key="window:1",
        )
    )
    try:
        first.start()
        assert started.wait(1)
        second.start()
        time.sleep(0.03)
        latest = scheduler.recognize_latest(image, request_key="window:1")
        release.set()
        first.join(1)
        second.join(1)
        assert first_error and isinstance(first_error[0], OCRRequestSuperseded)
        assert latest == [("frame-2", 0.99)]
        assert second_error and isinstance(second_error[0], OCRRequestSuperseded)
        assert len(calls) <= 2
    finally:
        release.set()
        first.join(1)
        second.join(1)
        scheduler.close(wait=True)


def _capture_error(target, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except Exception as exc:  # pragma: no cover - assertion checks the type
        target.append(exc)
