"""Bounded, credential-free metrics for logical ESI transport requests."""

import logging
import threading
import time
from collections import Counter, deque
from functools import wraps
from urllib.error import HTTPError
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class TransportMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.samples = deque(maxlen=1000)

    def record(self, *, mode, route, attempted, status, elapsed):
        success = 200 <= status < 400
        with self.lock:
            self.counts["requests"] += 1
            self.counts["attempts"] += int(attempted)
            self.counts["success"] += int(success)
            self.counts["final_errors"] += int(not success)
            self.counts["first_attempt_errors"] += int(attempted and not success)
            self.counts["budget_or_validation_errors"] += int(not attempted)
            self.samples.append((time.monotonic(), elapsed))
        logger.info(
            "esi_transport mode=%s route=%s attempted=%s status=%s elapsed_ms=%.1f retry=0 fallback=0",
            mode,
            route,
            attempted,
            status,
            elapsed,
        )

    def snapshot(self):
        with self.lock:
            values = sorted(
                value for at, value in self.samples if time.monotonic() - at <= 300
            )
            return {
                **self.counts,
                "retries": 0,
                "fallbacks": 0,
                "sample_window_seconds": 300,
                "sample_count": len(values),
                "sample_limit": 1000,
                "p50_ms": values[len(values) // 2] if values else None,
                "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))]
                if values
                else None,
                "max_ms": max(values) if values else None,
            }


def observe_transport(function):
    @wraps(function)
    def measured(self, request, *, timeout):
        started = time.monotonic()
        self.local.attempted = False
        status = 0
        parts = urlsplit(request.full_url).path.split("/")
        route = (
            "contacts"
            if "contacts" in parts
            else "affiliation"
            if "affiliation" in parts
            else "public"
        )
        try:
            result = function(self, request, timeout=timeout)
            status = result.status
            return result
        except HTTPError as exc:
            status = exc.code
            raise
        finally:
            self.telemetry.record(
                mode="relay" if self.relay_host else "direct",
                route=route,
                attempted=self.local.attempted,
                status=status,
                elapsed=(time.monotonic() - started) * 1000,
            )

    return measured
