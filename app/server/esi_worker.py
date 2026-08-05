"""Dedicated background worker for slow ESI enrichment tasks."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)


WORKER_IDLE_TIMEOUT_SECONDS = 0.5


class EsiWorker:
    """Run deduplicated ESI tasks on one lazily started daemon thread."""

    def __init__(self, handler: Callable[[Any], None]) -> None:
        self._handler = handler
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._pending_keys: set[str] = set()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, key: str, payload: Any) -> bool:
        """Queue one task unless the same key is already pending."""
        task_key = str(key or "").strip()
        if not task_key:
            raise ValueError("ESI task key is required")
        with self._lock:
            if self._closed or task_key in self._pending_keys:
                return False
            self._pending_keys.add(task_key)
            self._idle.clear()
            self._ensure_thread_locked()
            self._queue.put((task_key, payload))
        return True

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until all queued and running tasks have completed."""
        return self._idle.wait(timeout=timeout)

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting work and optionally wait for the worker to exit."""
        with self._lock:
            if self._closed:
                thread = self._thread
            else:
                self._closed = True
                thread = self._thread
                if thread is not None:
                    self._queue.put(None)
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join()

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="eve-sentry-esi-worker",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                task = self._queue.get(timeout=WORKER_IDLE_TIMEOUT_SECONDS)
            except queue.Empty:
                with self._lock:
                    if self._closed or not self._pending_keys:
                        if self._thread is threading.current_thread():
                            self._thread = None
                        self._idle.set()
                        return
                continue
            if task is None:
                self._queue.task_done()
                with self._lock:
                    if self._thread is threading.current_thread():
                        self._thread = None
                return
            key, payload = task
            try:
                self._handler(payload)
            except Exception:
                logger.exception("Unhandled ESI worker task failure: %s", key)
            finally:
                with self._lock:
                    self._pending_keys.discard(key)
                    if not self._pending_keys:
                        self._idle.set()
                self._queue.task_done()
