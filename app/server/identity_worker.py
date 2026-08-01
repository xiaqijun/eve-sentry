"""Persistent background worker for desktop identity verification jobs."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)


class IdentityVerificationWorker:
    """Claim durable identity jobs and process them outside HTTP request threads."""

    def __init__(
        self,
        claim: Callable[[str], dict[str, Any] | None],
        handler: Callable[[dict[str, Any], str], None],
        poll_interval: float = 1.0,
    ) -> None:
        self._claim = claim
        self._handler = handler
        self._poll_interval = max(0.05, float(poll_interval))
        self._worker_id = uuid.uuid4().hex
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop_requested = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="eve-sentry-identity-worker",
            daemon=True,
        )

    def start(self) -> None:
        """Start the worker once."""
        with self._start_lock:
            if self._started or self._stop_requested.is_set():
                return
            self._started = True
            self._thread.start()

    def wake(self) -> None:
        """Prompt the worker to check the durable queue immediately."""
        self._wake.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no job is currently executing."""
        return self._idle.wait(timeout=timeout)

    def close(self, *, wait: bool = True, timeout: float = 15.0) -> None:
        """Stop claiming work and optionally wait for the active job."""
        self._stop_requested.set()
        self._wake.set()
        if wait and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, float(timeout)))

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                job = self._claim(self._worker_id)
            except Exception:
                logger.exception("Could not claim an identity verification job")
                self._wait_for_work()
                continue
            if job is None:
                self._wait_for_work()
                continue
            self._idle.clear()
            try:
                self._handler(job, self._worker_id)
            except Exception:
                logger.exception(
                    "Unhandled identity verification job failure: %s",
                    job.get("job_id", "unknown"),
                )
            finally:
                self._idle.set()

    def _wait_for_work(self) -> None:
        self._wake.wait(self._poll_interval)
        self._wake.clear()
