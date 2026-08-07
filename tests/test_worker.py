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
            if len(self.screenshots) > 1:
                raise TargetWindowClosed("done")
            return Image.new("RGB", (w, h), color=(0, 0, 0))

        def close(self):
            self.closed = True

    class OriginalCapturer:
        def screenshot(self, x, y, w, h):
            raise AssertionError("worker should use the window-owned capturer")

    class FakeOcr:
        def recognize(self, image, progress=None):
            _ = image, progress
            raise AssertionError("a clear first frame must not run OCR")

    monkeypatch.setattr("app.engine.worker.Capturer", FakeOwnedCapturer)
    monkeypatch.setattr(MonitorWorker, "msleep", staticmethod(lambda _ms: None))

    worker = MonitorWorker(OriginalCapturer(), FakeOcr())
    worker.set_window({"hwnd": 42, "title": "EVE - Window", "w": 1280, "h": 720})
    worker.set_region(1000, 80, 260, 620)
    snapshots = []
    alerts = []
    worker.ocr_snapshot.connect(snapshots.append)
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert len(created_capturers) == 1
    owned = created_capturers[0]
    assert owned.selected == {
        "hwnd": 42,
        "title": "EVE - Window",
        "w": 1280,
        "h": 720,
    }
    assert owned.screenshots == [
        {"x": 1000, "y": 80, "w": 260, "h": 620},
        {"x": 1000, "y": 80, "w": 260, "h": 620},
    ]
    assert owned.closed is True
    assert snapshots == []
    assert alerts == [0]


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
            raise AssertionError("a frame without red icons must not run OCR")

    worker = MonitorWorker(FrameCapturer(), FriendlyOcr())
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    alerts = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert snapshots == []
    assert alerts == [0]


def test_monitor_worker_does_not_publish_full_list_after_red_row_mismatches():
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    draw.rectangle((6, 50, 16, 60), fill=(149, 8, 9))

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls <= 2:
                return frame
            raise TargetWindowClosed("done")

    class MismatchedOcr:
        def __init__(self):
            self.images = []

        def recognize(self, image, progress=None):
            _ = progress
            self.images.append(image)
            return [("Only One Red Name", 0.99)]

    ocr = MismatchedOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert snapshots == []
    assert len(ocr.images) == 1
    assert ocr.images[0].height < frame.height


def test_monitor_worker_resets_fallback_after_a_matching_red_row_frame():
    two_hostiles = Image.new("RGB", (180, 100), color=(12, 13, 13))
    draw = ImageDraw.Draw(two_hostiles)
    draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    draw.rectangle((6, 50, 16, 60), fill=(149, 8, 9))
    three_hostiles = two_hostiles.copy()
    ImageDraw.Draw(three_hostiles).rectangle((6, 75, 16, 85), fill=(152, 9, 8))
    frames = [two_hostiles, two_hostiles, three_hostiles]

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if frames:
                return frames.pop(0)
            raise TargetWindowClosed("done")

    class RecoveringOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, _image, progress=None):
            _ = progress
            self.calls += 1
            if self.calls == 1:
                return [("Only One Red Name", 0.99)]
            return [
                ("First Red", 0.99),
                ("Second Red", 0.99),
                ("Third Red", 0.99),
            ]

    ocr = RecoveringOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, two_hostiles.width, two_hostiles.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert snapshots == [(["First Red", "Second Red", "Third Red"], 3)]
    assert ocr.calls == 2


def test_monitor_worker_publishes_each_hostile_count_change_including_clear(
    monkeypatch,
):
    counts = iter([0, 2, 2, 1, 3, 3, 0])

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            try:
                return next(counts)
            except StopIteration:
                raise TargetWindowClosed("done") from None

    class StableOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, image, progress=None):
            _ = progress
            self.calls += 1
            return [
                (f"Enemy Pilot {index}", 0.99)
                for index in range(1, image + 1)
            ]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    monkeypatch.setattr(
        "app.engine.worker.find_hostile_icons",
        lambda count: [object()] * count,
    )
    monkeypatch.setattr(
        "app.engine.worker.extract_hostile_name_rows",
        lambda image: image,
    )
    ocr = StableOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, 180, 100)
    alerts = []
    snapshots = []
    worker.hostile_detected.connect(alerts.append)
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert alerts == [0, 2, 1, 3, 0]
    assert ocr.calls == 3
    assert [hostile_count for _names, hostile_count in snapshots] == [2, 1, 3]


def test_monitor_worker_reports_counts_without_ocr_when_disabled(monkeypatch):
    counts = iter([0, 2, 2, 3, 0])

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            try:
                return next(counts)
            except StopIteration:
                raise TargetWindowClosed("done") from None

    class ForbiddenOcr:
        def recognize(self, image, progress=None):
            _ = image, progress
            raise AssertionError("disabled OCR must never recognize a frame")

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    monkeypatch.setattr(
        "app.engine.worker.find_hostile_icons",
        lambda count: [object()] * count,
    )
    worker = MonitorWorker(FrameCapturer(), ForbiddenOcr())
    worker.set_region(0, 0, 180, 100)
    worker.set_ocr_enabled(False)
    alerts = []
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert alerts == [0, 2, 3, 0]


def test_monitor_worker_keeps_scan_interval_when_unchanged_frames_skip_ocr(
    monkeypatch,
):
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls <= 3:
                return frame
            raise TargetWindowClosed("done")

    class WarmUpOnlyOcr:
        def __init__(self):
            self.calls = 0

        def warm_up(self):
            self.calls += 1

        def recognize(self, _image, progress=None):
            _ = progress
            raise AssertionError("unchanged frames must skip OCR")

    waits = []
    monkeypatch.setattr(
        MonitorWorker,
        "_wait_for_next_scan",
        lambda self: waits.append(self._active_interval),
    )
    ocr = WarmUpOnlyOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_interval(2)
    worker.set_region(0, 0, frame.width, frame.height)
    statuses = []
    worker.status_update.connect(statuses.append)

    worker.run()

    assert ocr.calls == 0
    assert waits == [2, 2, 2]
    assert not any("OCR" in status for status in statuses)


def test_monitor_worker_keeps_configured_interval_for_benign_frame_changes(
    monkeypatch,
):
    frames = [
        Image.new("RGB", (180, 100), color=color)
        for color in ((12, 13, 13), (13, 13, 13), (14, 13, 13))
    ]

    class ChangingFrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            if frames:
                return frames.pop(0)
            raise TargetWindowClosed("done")

    class WarmUpOnlyOcr:
        def warm_up(self):
            return None

        def recognize(self, _image, progress=None):
            _ = progress
            raise AssertionError("benign frames must not run OCR")

    waits = []
    monkeypatch.setattr(
        MonitorWorker,
        "_wait_for_next_scan",
        lambda self: waits.append(self._active_interval),
    )
    worker = MonitorWorker(ChangingFrameCapturer(), WarmUpOnlyOcr())
    worker.set_interval(3)
    worker.set_region(0, 0, 180, 100)

    worker.run()

    assert waits == [3, 3, 3]


def test_monitor_worker_runs_ocr_once_while_hostile_count_remains_stable(monkeypatch):
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))
    ImageDraw.Draw(frame).rectangle((6, 20, 16, 30), fill=(146, 3, 3))

    class FrameCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls <= 7:
                return frame
            raise TargetWindowClosed("done")

    class RecordingOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, _image, progress=None):
            _ = progress
            self.calls += 1
            return [("Enemy Pilot", 0.99)]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    ocr = RecordingOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, frame.width, frame.height)
    statuses = []
    worker.status_update.connect(statuses.append)

    worker.run()

    assert ocr.calls == 1
    assert "画面无变化，已跳过 OCR" not in statuses


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

    assert statuses[-1] == "EVE 窗口已关闭，等待自动重连"


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
