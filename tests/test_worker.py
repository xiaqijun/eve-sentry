from app.engine.capturer import BackgroundCaptureUnavailable, TargetWindowClosed
from app.engine.worker import MonitorWorker, build_ocr_snapshot_names, build_scan_status


def test_scan_status_counts_cleaned_member_names_not_raw_ocr_blocks():
    ocr_results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        ("Bob", 0.95),
    ]

    assert build_scan_status(ocr_results) == (
        "名单识别: 2 个成员 / 2 个唯一 / 已上报服务器"
    )


def test_ocr_snapshot_names_are_cleaned_member_names():
    ocr_results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        ("alice", 0.90),
        ("Bob, + Carol", 0.88),
    ]

    assert build_ocr_snapshot_names(ocr_results) == ["Alice", "Bob", "Carol"]


def test_monitor_worker_uses_bound_window_capture_session(monkeypatch):
    created_capturers = []

    class FakeOwnedCapturer:
        def __init__(self):
            self.selected = None
            self.screenshots = []
            self.closed = False
            created_capturers.append(self)

        def select_window(self, hwnd, title, w, h):
            self.selected = {
                "hwnd": hwnd,
                "title": title,
                "w": w,
                "h": h,
            }

        def screenshot(self, x, y, w, h):
            self.screenshots.append({"x": x, "y": y, "w": w, "h": h})
            return object()

        def close(self):
            self.closed = True

    class OriginalCapturer:
        def screenshot(self, x, y, w, h):
            raise AssertionError("worker should use the window-owned capturer")

    class FakeOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, image, progress=None):
            _ = image, progress
            self.calls += 1
            worker.stop()
            return []

    monkeypatch.setattr("app.engine.worker.Capturer", FakeOwnedCapturer)
    monkeypatch.setattr(MonitorWorker, "msleep", staticmethod(lambda _ms: None))

    worker = MonitorWorker(OriginalCapturer(), FakeOcr())
    worker.set_window({"hwnd": 42, "title": "EVE - Window", "w": 1280, "h": 720})
    worker.set_region(1000, 80, 260, 620)
    snapshots = []
    worker.ocr_snapshot.connect(snapshots.append)

    worker.run()

    assert len(created_capturers) == 1
    owned = created_capturers[0]
    assert owned.selected == {
        "hwnd": 42,
        "title": "EVE - Window",
        "w": 1280,
        "h": 720,
    }
    assert owned.screenshots == [{"x": 1000, "y": 80, "w": 260, "h": 620}]
    assert owned.closed is True
    assert snapshots == []


def test_monitor_worker_stops_when_the_game_window_closes():
    class ClosedCapturer:
        def screenshot(self, _x, _y, _w, _h):
            raise TargetWindowClosed("closed")

    class ForbiddenOcr:
        def recognize(self, _image, progress=None):
            _ = progress
            raise AssertionError("closed game window must not reach OCR")

    worker = MonitorWorker(ClosedCapturer(), ForbiddenOcr())
    worker.set_region(1000, 80, 260, 620)
    statuses = []
    worker.status_update.connect(statuses.append)

    worker.run()

    assert statuses[-1] == "EVE 窗口已关闭，监控已停止"


def test_monitor_worker_skips_ocr_when_background_frame_is_unavailable():
    class UnavailableCapturer:
        def screenshot(self, _x, _y, _w, _h):
            worker.stop()
            raise BackgroundCaptureUnavailable("no frame")

    class ForbiddenOcr:
        def recognize(self, _image, progress=None):
            _ = progress
            raise AssertionError("unavailable background frame must not reach OCR")

    worker = MonitorWorker(UnavailableCapturer(), ForbiddenOcr())
    worker.set_region(1000, 80, 260, 620)
    statuses = []
    worker.status_update.connect(statuses.append)

    worker.run()

    assert statuses[-1] == "后台画面暂不可用，已跳过当前帧"
