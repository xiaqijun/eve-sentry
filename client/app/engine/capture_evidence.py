"""Small same-frame capture metadata; images/coordinates stay on the client."""

import time
from uuid import uuid4


def new_capture_session() -> dict:
    return {"session_id": uuid4().hex, "session_epoch": time.time_ns()}


def roster_quality(image, boxes) -> str:
    """Conservative clipping evidence, not an assertion of OCR name accuracy."""
    if not boxes or not hasattr(image, "size"):
        return "unknown"
    width, height = image.size
    for _text, _confidence, bounds in boxes:
        x1, y1, x2, y2 = bounds
        if x1 <= 1 or y1 <= 1 or x2 >= width - 1 or y2 >= height - 1:
            return "truncated"
    return "complete"
