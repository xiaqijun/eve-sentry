"""Simple process-wide interval limiter for ESI upstream requests."""

from __future__ import annotations

import time
from threading import Lock


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.requests_per_second = max(0.1, float(requests_per_second))
        self.interval = 1.0 / self.requests_per_second
        self._lock = Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
