from PIL import Image, ImageDraw

from app.engine.capturer import BackgroundCaptureUnavailable, TargetWindowClosed
from app.engine.worker import (
    MonitorWorker,
    _image_fingerprint,
    build_ocr_snapshot_names,
    build_scan_status,
)


def test_scan_status_counts_raw_ocr_blocks():
    ocr_results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        ("Bob", 0.95),
    ]

    assert build_scan_status(ocr_results) == "OCR 识别完成: 3 个文本候选，已进入上报队列"


def test_ocr_snapshot_names_keep_complete_raw_ocr_results():
    ocr_results = [
        ("+", 0.95),
        ("+ Alice", 0.95),
        ("alice", 0.90),
        ("Bob, + Carol", 0.88),
        ("= Zana Fehrnah", 0.97),
    ]

    assert build_ocr_snapshot_names(ocr_results) == [
        "Alice",
        "Bob",
        "Carol",
        "Zana Fehrnah",
    ]


def test_monitor_worker_matches_full_frame_ocr_by_hostile_icon_row(monkeypatch):
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

    class PositionedOcr:
        def recognize_with_boxes(self, _image, progress=None):
            _ = progress
            return [
                ("Friendly Pilot", 0.99, (20, 4, 100, 14)),
                ("STARKEY 07", 0.99, (20, 20, 100, 30)),
                ("Second Pilot", 0.99, (20, 50, 100, 60)),
            ]

        def recognize(self, _image, progress=None):
            raise AssertionError("full-frame OCR should not fall back to row crops")

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    worker = MonitorWorker(FrameCapturer(), PositionedOcr())
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert snapshots == [
        (["Friendly Pilot", "STARKEY 07", "Second Pilot"], 2),
    ]


def test_hostile_row_fingerprint_ignores_non_name_columns():
    first = Image.new("RGB", (200, 40), color=(12, 13, 13))
    second = first.copy()
    ImageDraw.Draw(first).rectangle((4, 10, 16, 20), fill=(220, 220, 220))
    ImageDraw.Draw(second).rectangle((180, 10, 196, 20), fill=(220, 220, 220))

    assert _image_fingerprint(first) == _image_fingerprint(second)


def test_hostile_row_fingerprint_ignores_extraction_border_jitter():
    first = Image.new("RGB", (200, 40), color=(12, 13, 13))
    second = Image.new("RGB", (201, 40), color=(12, 13, 13))
    ImageDraw.Draw(first).rectangle((30, 10, 95, 20), fill=(220, 220, 220))
    ImageDraw.Draw(second).rectangle((31, 10, 96, 20), fill=(220, 220, 220))

    assert _image_fingerprint(first) == _image_fingerprint(second)


def test_monitor_worker_does_not_repeat_ocr_for_border_jitter(monkeypatch):
    frames = [0, 1]

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            if frames:
                return frames.pop(0)
            raise TargetWindowClosed("done")

    class RecordingOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, _image, progress=None):
            _ = progress
            self.calls += 1
            return [("Enemy Pilot", 0.99)]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    monkeypatch.setattr("app.engine.worker.find_hostile_icons", lambda _frame: [object()])
    ocr = RecordingOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, 200, 40)

    worker.run()

    assert ocr.calls == 2


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


def test_monitor_worker_ocr_uploads_the_complete_captured_roster():
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
    assert ocr.images[0].height == frame.height
    assert ocr.images[0].width == frame.width


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
            return [("Only One Red Name", 0.99)] if len(self.images) % 2 else []

    ocr = MismatchedOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert snapshots == [(["Only One Red Name"], 2)]
    assert len(ocr.images) == 1
    assert ocr.images[0].height == frame.height


def test_monitor_worker_retries_same_count_when_hostile_rows_change(monkeypatch):
    first = Image.new("RGB", (180, 100), color=(12, 13, 13))
    second = first.copy()
    first_draw = ImageDraw.Draw(first)
    first_draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    first_draw.rectangle((20, 22, 90, 28), fill=(220, 220, 220))
    second_draw = ImageDraw.Draw(second)
    second_draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    second_draw.rectangle((20, 22, 65, 28), fill=(220, 220, 220))
    frames = iter([first, second])

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            try:
                return next(frames)
            except StopIteration:
                raise TargetWindowClosed("done") from None

    class StableOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, _image, progress=None):
            _ = progress
            self.calls += 1
            return [("Enemy Pilot", 0.99)]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    ocr = StableOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, first.width, first.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert ocr.calls == 2
    assert snapshots == [(["Enemy Pilot"], 1), (["Enemy Pilot"], 1)]


def test_monitor_worker_retries_transient_name_count_mismatch(monkeypatch):
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

    class RecoveringOcr:
        def __init__(self):
            self.calls = 0

        def recognize(self, _image, progress=None):
            _ = progress
            self.calls += 1
            if self.calls == 1:
                return []
            return [
                ("First Red", 0.99)
                if self.calls == 3
                else ("Second Red", 0.99)
            ]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    ocr = RecoveringOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, frame.width, frame.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert ocr.calls == 2
    assert snapshots == [(["Second Red"], 2)]


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
            names = {
                1: "Only One Red Name",
                3: "First Red",
                4: "Second Red",
                5: "First Red",
                6: "Second Red",
                7: "Third Red",
            }
            return [(names[self.calls], 0.99)] if self.calls in names else []

    ocr = RecoveringOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, two_hostiles.width, two_hostiles.height)
    snapshots = []
    worker.ocr_snapshot.connect(
        lambda names, hostile_count: snapshots.append((names, hostile_count))
    )

    worker.run()

    assert snapshots == [(["Only One Red Name"], 2)]
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
            return [(f"Enemy Pilot {image}", 0.99)]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    monkeypatch.setattr(
        "app.engine.worker.find_hostile_icons",
        lambda count: [object()] * count,
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


def test_monitor_worker_republishes_unchanged_count_after_presence_refresh(
    monkeypatch,
):
    counts = iter([1, 1])
    worker = None

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            nonlocal worker
            try:
                count = next(counts)
            except StopIteration:
                raise TargetWindowClosed("done") from None
            if count == 1 and worker is not None and worker._burst_scans_remaining:
                worker.request_presence_refresh()
            return count

    class StableOcr:
        def recognize(self, image, progress=None):
            _ = progress
            return [("Enemy Pilot", 0.99)] * image

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    monkeypatch.setattr(
        "app.engine.worker.find_hostile_icons",
        lambda count: [object()] * count,
    )
    worker = MonitorWorker(FrameCapturer(), StableOcr())
    worker.set_region(0, 0, 180, 100)
    alerts = []
    worker.hostile_detected.connect(alerts.append)

    worker.run()

    assert alerts == [1, 1]


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


def test_monitor_worker_emits_periodic_health_status_for_unchanged_frames(
    monkeypatch,
):
    frames = iter([0, 0])
    clock = iter([0.0, 16.0, 16.0])

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            try:
                return next(frames)
            except StopIteration:
                raise TargetWindowClosed("done") from None

    class ForbiddenOcr:
        def recognize(self, _image, progress=None):
            _ = progress
            raise AssertionError("unchanged clear frames must not run OCR")

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    monkeypatch.setattr("app.engine.worker.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("app.engine.worker.find_hostile_icons", lambda count: [])
    worker = MonitorWorker(FrameCapturer(), ForbiddenOcr())
    worker.set_region(0, 0, 180, 100)
    statuses = []
    worker.status_update.connect(statuses.append)

    worker.run()

    assert "未检测到敌对图标" in statuses
    assert "持续监测中: 未检测到敌对图标" in statuses


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


def test_full_frame_ocr_does_not_repeat_for_changes_outside_hostile_rows(monkeypatch):
    frames = []
    for background in ((12, 13, 13), (20, 21, 22), (30, 31, 32)):
        frame = Image.new("RGB", (180, 100), color=background)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
        draw.rectangle((20, 22, 90, 28), fill=(220, 220, 220))
        frames.append(frame)

    class FrameCapturer:
        def screenshot(self, _x, _y, _w, _h):
            if frames:
                return frames.pop(0)
            raise TargetWindowClosed("done")

    class PositionedOcr:
        def __init__(self):
            self.calls = 0

        def recognize_with_boxes(self, _image, progress=None):
            _ = progress
            self.calls += 1
            return [("Enemy Pilot", 0.99, (17, 20, 100, 30))]

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    ocr = PositionedOcr()
    worker = MonitorWorker(FrameCapturer(), ocr)
    worker.set_region(0, 0, 180, 100)

    worker.run()

    assert ocr.calls == 1


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
    lost = []
    worker.status_update.connect(statuses.append)
    worker.connection_lost.connect(lost.append)

    worker.run()

    assert statuses[-1] == "EVE 窗口已关闭，等待自动重连"
    assert lost == ["EVE 窗口已关闭，等待自动重连"]


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


def test_monitor_worker_requires_repeated_capture_failures_and_reports_recovery(
    monkeypatch,
):
    frame = Image.new("RGB", (180, 100), color=(12, 13, 13))

    class FlakyCapturer:
        def __init__(self):
            self.calls = 0

        def screenshot(self, _x, _y, _w, _h):
            self.calls += 1
            if self.calls <= 3:
                raise BackgroundCaptureUnavailable("no frame")
            worker.stop()
            return frame

    class ForbiddenOcr:
        def recognize(self, image, progress=None):
            _ = image, progress
            raise AssertionError("a clear recovery frame must not reach OCR")

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: None)
    worker = MonitorWorker(FlakyCapturer(), ForbiddenOcr())
    worker.set_region(1000, 80, 260, 620)
    lost = []
    restored = []
    worker.connection_lost.connect(lost.append)
    worker.connection_restored.connect(lambda: restored.append(True))

    worker.run()

    assert lost == ["后台画面连续不可用"]
    assert restored == [True]
