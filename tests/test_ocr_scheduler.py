import threading
import time

from PIL import Image

from app.engine.ocr_scheduler import SharedOCRScheduler


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
