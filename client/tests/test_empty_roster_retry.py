"""Icon-only crops publish failed OCR evidence and retry unchanged images."""

from PIL import Image, ImageDraw

from app.engine.capturer import TargetWindowClosed
from app.engine.worker import MonitorWorker


def test_empty_ocr_is_reported_and_retried_without_count_change(monkeypatch):
    image = Image.new("RGB", (27, 100), color=(12, 13, 13))
    ImageDraw.Draw(image).rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    clock = [100.0]
    monkeypatch.setattr("app.engine.worker.time.monotonic", lambda: clock[0])

    class Capture:
        calls = 0

        def screenshot(self, *args):
            self.calls += 1
            if self.calls > 8:
                raise TargetWindowClosed("done")
            return image

    class EmptyOCR:
        def recognize_with_boxes(self, image, progress=None):
            return []

    monkeypatch.setattr(MonitorWorker, "_wait_for_next_scan", lambda self: clock.__setitem__(0, clock[0] + 1))
    worker = MonitorWorker(Capture(), EmptyOCR())
    worker.set_region(0, 0, 27, 100)
    snapshots = []
    statuses = []
    worker.ocr_evidence_snapshot.connect(lambda names, count, evidence: snapshots.append((names, count, evidence)))
    worker.status_update.connect(statuses.append)
    worker.run()
    assert len(snapshots) == 4  # Three fast attempts, then a five-second retry.
    assert all(names == [] and count == 1 for names, count, _ in snapshots)
    captures = [evidence["capture"] for _, _, evidence in snapshots]
    assert all(capture["roster_quality"] == "unknown" for capture in captures)
    assert captures[1]["sequence"] > captures[0]["sequence"]
    assert any("完整姓名列" in text for text in statuses)
