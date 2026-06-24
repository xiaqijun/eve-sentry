"""OCR wrapper using PaddleOCR."""

import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)


class OCREngine:
    """Wraps PaddleOCR for text recognition on screen captures.

    Initialises the model once at construction time.  All recognition
    calls are synchronous (they run on the worker thread so they won't
    block the UI).
    """

    def __init__(
        self,
        lang: str = "ch",
        confidence_threshold: float = 0.7,
    ):
        self._confidence_threshold = confidence_threshold
        self._ocr: Optional[object] = None
        self._lang = lang
        self._init_ocr()

    def _init_ocr(self) -> None:
        """Lazy-init the PaddleOCR instance (expensive)."""
        try:
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(lang=self._lang, use_angle_cls=False)
            logger.info("PaddleOCR initialised (lang=%s)", self._lang)
        except Exception:
            logger.exception("Failed to initialise PaddleOCR")
            self._ocr = None

    def recognize(self, image: Image.Image) -> list[tuple[str, float]]:
        """Run OCR on *image* and return high-confidence text lines.

        Each element is ``(text, confidence)`` where ``text`` is the
        recognised string and ``confidence`` is a float in [0, 1].

        Returns an empty list when the OCR engine is unavailable or
        recognition fails.
        """
        if self._ocr is None:
            return []

        # Pre-process: convert to grayscale for better accuracy on game text
        if image.mode != "L":
            image = image.convert("L")

        try:
            raw = self._ocr.ocr(image, cls=False)
        except Exception:
            logger.exception("OCR recognition failed")
            return []

        if raw is None or len(raw) == 0:
            return []

        results: list[tuple[str, float]] = []
        # raw[0] is list of [bbox, (text, confidence)] per detected text block
        for block in raw[0]:
            if block is None:
                continue
            _, info = block  # info is (text, confidence)
            text, conf = info[0], float(info[1])
            if conf >= self._confidence_threshold:
                results.append((text.strip(), conf))

        return results
