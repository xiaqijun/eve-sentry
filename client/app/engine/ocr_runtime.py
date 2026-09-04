"""Lightweight OCR runtime preloading without creating model sessions."""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


def preload_ocr_runtime(backend: str | None = None) -> bool:
    """Import the ONNX OCR stack ahead of monitoring without loading models."""
    selected = (
        backend or os.environ.get("EVE_SENTRY_OCR_BACKEND", "paddle")
    ).strip().lower()
    if selected != "onnx":
        return False

    started = time.monotonic()
    try:
        _import_onnx_runtime()
    except Exception:
        logger.warning("Unable to preload the ONNX OCR runtime", exc_info=True)
        return False
    logger.info(
        "ONNX OCR runtime preloaded in %.2f seconds; model sessions remain lazy",
        time.monotonic() - started,
    )
    return True


def _import_onnx_runtime() -> None:
    """Import runtime modules while keeping heavyweight sessions uninitialised."""
    import onnxruntime  # noqa: F401
    from rapidocr import RapidOCR  # noqa: F401
