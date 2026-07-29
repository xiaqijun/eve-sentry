"""Shared, lazy OCR inference pool for all monitored EVE windows."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PIL import Image

from app.engine.ocr import OCREngine


class SharedOCRScheduler:
    """Bound OCR model count while allowing many capture workers to share it."""

    def __init__(
        self,
        max_instances: int | None = None,
        engine_factory: Callable[[], OCREngine] | None = None,
    ) -> None:
        configured = max_instances
        if configured is None:
            configured = _env_int("EVE_SENTRY_OCR_INSTANCES", 1)
        self.max_instances = max(1, min(2, int(configured)))
        self._engine_factory = engine_factory or (
            lambda: OCREngine(lang="en", confidence_threshold=0.7)
        )
        self._local = threading.local()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_instances,
            thread_name_prefix="eve-sentry-ocr",
        )
        self._closed = False
        self._models_loaded = 0
        self._completed = 0
        self._failed = 0
        self._last_latency_ms = 0.0
        self._last_success_at = 0.0

    def recognize(self, image: Image.Image, progress=None) -> list[tuple[str, float]]:
        """Run OCR on the bounded inference pool and return its result."""
        with self._lock:
            if self._closed:
                raise RuntimeError("OCR scheduler is closed")
        return self._executor.submit(self._recognize, image, progress).result()

    def warm_up(self) -> None:
        """Load one model asynchronously after monitoring has started."""
        with self._lock:
            if self._closed:
                return
        self._executor.submit(self._engine)

    def health(self) -> dict[str, float | int | str]:
        """Return a small diagnostics snapshot without exposing model details."""
        with self._lock:
            if self._closed:
                state = "stopped"
            elif self._models_loaded:
                state = "ready"
            else:
                state = "loading"
            return {
                "state": state,
                "models_loaded": self._models_loaded,
                "max_instances": self.max_instances,
                "completed": self._completed,
                "failed": self._failed,
                "last_latency_ms": round(self._last_latency_ms, 1),
                "last_success_at": self._last_success_at,
            }

    def close(self, wait: bool = False) -> None:
        """Cancel queued jobs and release model instances with executor threads."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _engine(self) -> OCREngine:
        engine = getattr(self._local, "engine", None)
        if engine is None:
            engine = self._engine_factory()
            engine.initialize()
            self._local.engine = engine
            with self._lock:
                self._models_loaded += 1
        return engine

    def _recognize(self, image: Image.Image, progress) -> list[tuple[str, float]]:
        started = time.monotonic()
        try:
            result = self._engine().recognize(image, progress=progress)
        except Exception:
            with self._lock:
                self._failed += 1
            raise
        with self._lock:
            self._completed += 1
            self._last_latency_ms = (time.monotonic() - started) * 1000
            self._last_success_at = time.time()
        return result


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
