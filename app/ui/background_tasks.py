"""Small coalescing background-task runner for the desktop UI."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal


class BackgroundTaskRunner(QObject):
    """Run blocking work off the Qt event loop and coalesce repeated jobs."""

    completed = pyqtSignal(str, object, object)

    def __init__(self, max_workers: int = 2, parent: QObject | None = None):
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="eve-sentry-io",
        )
        self._pending: set[str] = set()
        self._latest: dict[str, tuple[Callable[[], Any], object]] = {}

    def submit_once(
        self,
        key: str,
        task: Callable[[], Any],
        context: object = None,
    ) -> bool:
        """Submit one job unless the same key is already running."""
        if key in self._pending:
            return False
        self._start(key, task, context)
        return True

    def submit_latest(
        self,
        key: str,
        task: Callable[[], Any],
        context: object = None,
    ) -> bool:
        """Run the newest job for a key without building an unbounded queue."""
        if key in self._pending:
            self._latest[key] = (task, context)
            return False
        self._start(key, task, context)
        return True

    def finish(self, key: str) -> bool:
        """Mark a completed job and start its newest coalesced replacement."""
        self._pending.discard(key)
        latest = self._latest.pop(key, None)
        if latest is None:
            return False
        task, context = latest
        self._start(key, task, context)
        return True

    def shutdown(self) -> None:
        """Stop accepting work and cancel jobs that have not started."""
        self._latest.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def cancel_latest(self) -> None:
        """Discard coalesced replacements while allowing running jobs to finish."""
        self._latest.clear()

    def _start(
        self,
        key: str,
        task: Callable[[], Any],
        context: object,
    ) -> None:
        self._pending.add(key)
        future = self._executor.submit(task)
        future.add_done_callback(
            lambda completed, job_key=key, job_context=context: self._emit_completed(
                job_key,
                completed,
                job_context,
            )
        )

    def _emit_completed(
        self,
        key: str,
        future: Future,
        context: object,
    ) -> None:
        try:
            self.completed.emit(key, future, context)
        except RuntimeError:
            return
