from PIL import Image, ImageDraw

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
            return Image.new("RGB", (w, h), color=(0, 0, 0))

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


def test_monitor_worker_only_sends_verified_red_icon_rows_to_ocr():
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    draw.rectangle((9, 25, 13, 25), fill=(255, 255, 255))
    draw.rectangle((6, 35, 16, 45), fill=(18, 130, 45))

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls == 1:
                return frame
            raise TargetWindowClosed("done")

    class RecordingOcr:
        def __init__(self):
            self.images = []

        def recognize(self, image, progress=None):
            _ = progress
            self.images.append(image)
            return [("Enemy Pilot", 0.99)]

    ocr = RecordingOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    alerts = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert snapshots == [(["Enemy Pilot"], 1)]
    assert alerts == [1]
    assert len(ocr.images) == 1
    assert ocr.images[0].height < frame.height
    assert ocr.images[0].width < frame.width


def test_monitor_worker_discards_ocr_names_when_no_red_icon_exists():
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((6, 20, 16, 30), fill=(18, 130, 45))

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls == 1:
                return frame
            raise TargetWindowClosed("done")

    class FriendlyOcr:
        def recognize(self, image, progress=None):
            _ = image, progress
            return [("Friendly Pilot", 0.99)]

    worker = MonitorWorker(FrameCapturer(), FriendlyOcr())
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    alerts = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert snapshots == [([], 0)]
    assert alerts == []


def test_monitor_worker_falls_back_to_full_ocr_when_red_rows_do_not_match():
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    draw.rectangle((6, 50, 16, 60), fill=(149, 8, 9))

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls == 1:
                return frame
            raise TargetWindowClosed("done")

    class MismatchedOcr:
        def __init__(self):
            self.images = []

        def recognize(self, image, progress=None):
            _ = progress
            self.images.append(image)
            if len(self.images) == 1:
                return [("Only One Red Name", 0.99)]
            return [("Friendly Pilot", 0.99), ("Enemy Pilot", 0.99)]

    ocr = MismatchedOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert snapshots == [(["Friendly Pilot", "Enemy Pilot"], 0)]
    assert len(ocr.images) == 2
    assert ocr.images[0].height < frame.height
    assert ocr.images[1].size == frame.size


def test_monitor_worker_hostile_alert_is_edge_triggered_and_resets_after_clear(
    monkeypatch,
):
    clear = Image.new("RGB", (180, 100), color=(12, 13, 13))
    hostile = clear.copy()
    ImageDraw.Draw(hostile).rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    frames = [hostile, hostile, clear, hostile]

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            if frames:
                return frames.pop(0)
            raise TargetWindowClosed("done")

    class StableOcr:
        def recognize(self, image, progress=None):
            _ = image, progress
            return [("Enemy Pilot", 0.99)]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    worker = MonitorWorker(FrameCapturer(), StableOcr())
    worker.set_region(0, 0, hostile.width, hostile.height)
    alerts = []
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert alerts == [1, 1]


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
