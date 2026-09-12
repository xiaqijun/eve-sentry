"""Quality describes frame clipping, never OCR identity resolution."""

from PIL import Image

from app.engine.capture_evidence import new_capture_session, roster_quality


def test_quality_uses_complete_frame_geometry():
    image = Image.new("RGB", (300, 200))
    assert roster_quality(image, []) == "unknown"
    assert roster_quality(image, [("Name", 0.9, (20, 20, 100, 40))]) == "complete"
    assert roster_quality(image, [("Name", 0.9, (20, 180, 100, 200))]) == "truncated"
    assert roster_quality(image, [("Name", 0.9, (20, 20, 299, 40))]) == "truncated"


def test_monitor_restarts_get_distinct_sessions():
    first, second = new_capture_session(), new_capture_session()
    assert first["session_id"] != second["session_id"]
    assert second["session_epoch"] >= first["session_epoch"]
