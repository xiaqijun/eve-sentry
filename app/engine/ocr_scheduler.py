"""Shared, lazy OCR inference pool for all monitored EVE windows."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Callable

from PIL import Image

from app.engine.ocr import OCREngine


class OCRRequestSuperseded(RuntimeError):
    """Raised when a newer frame replaced a queued OCR request."""


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
        self._job_lock = threading.RLock()
        self._latest_generation: dict[str, int] = {}
        self._jobs: dict[Future, tuple[str, int, int]] = {}

    def recognize(self, image: Image.Image, progress=None) -> list[tuple[str, float]]:
        """Run OCR on the bounded inference pool and return its result."""
        with self._lock:
            if self._closed:
                raise RuntimeError("OCR scheduler is closed")
        return self._executor.submit(self._recognize, image, progress).result()

    def recognize_latest(
        self,
        image: Image.Image,
        progress=None,
        *,
        request_key: str,
        priority: int = 0,
    ) -> list[tuple[str, float]]:
        """Run only the newest queued request for a window.

        A running model invocation is allowed to finish, but queued older frames
        are cancelled before they consume an inference slot.
        """
        key = str(request_key or "window")
        with self._lock:
            if self._closed:
                raise RuntimeError("OCR scheduler is closed")
        with self._job_lock:
            generation = self._latest_generation.get(key, 0) + 1
            self._latest_generation[key] = generation
            for future, (job_key, _job_generation, job_priority) in list(
                self._jobs.items()
            ):
                if future.done():
                    self._jobs.pop(future, None)
                    continue
                if job_key == key or priority > job_priority:
                    future.cancel()
            future = self._executor.submit(
                self._recognize_latest,
                key,
                generation,
                int(priority),
                image,
                progress,
            )
            self._jobs[future] = (key, generation, int(priority))
            future.add_done_callback(self._forget_job)
        try:
            return future.result()
        except CancelledError as exc:
            raise OCRRequestSuperseded from exc

    def recognize_with_boxes_latest(
        self,
        image: Image.Image,
        progress=None,
        *,
        request_key: str,
        priority: int = 0,
    ):
        """Run the newest OCR request and retain geometry for local consumers."""
        key = str(request_key or "window")
        with self._lock:
            if self._closed:
                raise RuntimeError("OCR scheduler is closed")
        with self._job_lock:
            generation = self._latest_generation.get(key, 0) + 1
            self._latest_generation[key] = generation
            for future, (job_key, _job_generation, job_priority) in list(
                self._jobs.items()
            ):
                if future.done():
                    self._jobs.pop(future, None)
                    continue
                if job_key == key or priority > job_priority:
                    future.cancel()
            future = self._executor.submit(
                self._recognize_with_boxes_latest,
                key,
                generation,
                int(priority),
                image,
                progress,
            )
            self._jobs[future] = (key, generation, int(priority))
            future.add_done_callback(self._forget_job)
        try:
            return future.result()
        except CancelledError as exc:
            raise OCRRequestSuperseded from exc

    def _recognize_latest(
        self,
        key: str,
        generation: int,
        _priority: int,
        image: Image.Image,
        progress,
    ) -> list[tuple[str, float]]:
        with self._job_lock:
            if generation != self._latest_generation.get(key):
                raise OCRRequestSuperseded
        result = self._recognize(image, progress)
        with self._job_lock:
            if generation != self._latest_generation.get(key):
                raise OCRRequestSuperseded
        return result

    def _recognize_with_boxes_latest(
        self,
        key: str,
        generation: int,
        _priority: int,
        image: Image.Image,
        progress,
    ):
        with self._job_lock:
            if generation != self._latest_generation.get(key):
                raise OCRRequestSuperseded
        result = self._recognize_with_boxes(image, progress)
        with self._job_lock:
            if generation != self._latest_generation.get(key):
                raise OCRRequestSuperseded
        return result

    def _forget_job(self, future: Future) -> None:
        with self._job_lock:
            self._jobs.pop(future, None)

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
        with self._job_lock:
            self._jobs.clear()
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

    def _recognize_with_boxes(self, image: Image.Image, progress):
        started = time.monotonic()
        try:
            result = self._engine().recognize_with_boxes(image, progress=progress)
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
