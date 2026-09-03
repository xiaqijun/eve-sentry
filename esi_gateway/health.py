"""Redacted health and request metrics for the gateway."""

from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any


def _metric() -> dict[str, float | int]:
    return {"requests": 0, "cache_hits": 0, "cache_misses": 0, "upstream_requests": 0, "errors": 0, "total_latency": 0.0, "last_latency": 0.0}


class HealthMetrics:
    def __init__(self, service: str = "eve-sentry-esi-gateway", version: str = "1.0") -> None:
        self.service = service
        self.version = version
        self.started_at = time.monotonic()
        self._lock = RLock()
        self._request_times: deque[float] = deque(maxlen=10000)
        self.requests = self.upstream_requests = self.cache_hits = self.cache_misses = self.errors = 0
        self.coalesced = 0
        self.negative_hits = self.stale_served = 0
        self.hot_hits = self.durable_hits = self.stale_hits = 0
        self.refresh_queued = self.refresh_batches = 0
        self.refresh_success = self.refresh_failures = self.refresh_retries = 0
        self.postgres_errors = self.redis_errors = 0
        self.total_latency = self.last_latency = 0.0
        self.last_error_at: float | None = None
        self.endpoints: dict[str, dict[str, float | int]] = {}

    def record_request(self, endpoint: str, *, cached: bool) -> None:
        with self._lock:
            self.requests += 1
            self._request_times.append(time.monotonic())
            metric = self.endpoints.setdefault(endpoint, _metric())
            metric["requests"] += 1
            metric["cache_hits" if cached else "cache_misses"] += 1

    def record_upstream(self, endpoint: str, duration: float) -> None:
        with self._lock:
            self.upstream_requests += 1
            self.total_latency += duration
            self.last_latency = duration
            metric = self.endpoints.setdefault(endpoint, _metric())
            metric["upstream_requests"] += 1
            metric["total_latency"] += duration
            metric["last_latency"] = duration

    def record_error(self, endpoint: str) -> None:
        with self._lock:
            self.errors += 1
            self.last_error_at = time.time()
            self.endpoints.setdefault(endpoint, _metric())["errors"] += 1

    def _increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + amount)

    def record_hot_hits(self, amount: int = 1) -> None:
        self._increment("hot_hits", amount)

    def record_durable_hits(self, amount: int = 1) -> None:
        self._increment("durable_hits", amount)

    def record_stale_hits(self, amount: int = 1) -> None:
        self._increment("stale_hits", amount)

    def record_refresh_queued(self, amount: int = 1) -> None:
        self._increment("refresh_queued", amount)

    def record_refresh_batches(self, amount: int = 1) -> None:
        self._increment("refresh_batches", amount)

    def record_refresh_success(self, amount: int = 1) -> None:
        self._increment("refresh_success", amount)

    def record_refresh_failures(self, amount: int = 1) -> None:
        self._increment("refresh_failures", amount)

    def record_refresh_retries(self, amount: int = 1) -> None:
        self._increment("refresh_retries", amount)

    def record_postgres_errors(self, amount: int = 1) -> None:
        self._increment("postgres_errors", amount)

    def record_redis_errors(self, amount: int = 1) -> None:
        self._increment("redis_errors", amount)

    def snapshot(
        self,
        cache_entries: int,
        rate_limit: float,
        *,
        cache_evictions: int = 0,
        inflight: int = 0,
        negative_hits: int = 0,
        negative_entries: int = 0,
        stale_served: int = 0,
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            uptime = max(0.0, now - self.started_at)
            window = max(1.0, min(60.0, uptime))
            rate = sum(t >= now - window for t in self._request_times) / window
            endpoints = {}
            for name, metric in self.endpoints.items():
                endpoints[name] = {
                    "requests": metric["requests"], "cache_hits": metric["cache_hits"], "cache_misses": metric["cache_misses"],
                    "upstream_requests": metric["upstream_requests"], "errors": metric["errors"],
                    "cache_hit_rate": round(metric["cache_hits"] / max(1, metric["requests"]), 4),
                    "last_latency_ms": round(float(metric["last_latency"]) * 1000, 2),
                    "average_latency_ms": round(float(metric["total_latency"]) / max(1, metric["upstream_requests"]) * 1000, 2),
                }
            return {
                "ok": True, "service": self.service, "version": self.version,
                "uptime_seconds": round(uptime, 1), "requests": self.requests, "total_requests": self.requests,
                "upstream_requests": self.upstream_requests, "cache_misses": self.cache_misses,
                "cache_hits": self.cache_hits, "errors": self.errors, "cache_entries": cache_entries,
                "cache_evictions": cache_evictions, "inflight": inflight,
                "coalesced": self.coalesced,
                "negative_hits": negative_hits, "negative_entries": negative_entries,
                "stale_served": stale_served,
                "cache_hit_rate": round(self.cache_hits / max(1, self.requests), 4),
                "id_cache": {
                    "hot_hits": self.hot_hits,
                    "durable_hits": self.durable_hits,
                    "stale_hits": self.stale_hits,
                    "refresh_queued": self.refresh_queued,
                    "refresh_batches": self.refresh_batches,
                    "refresh_success": self.refresh_success,
                    "refresh_failures": self.refresh_failures,
                    "refresh_retries": self.refresh_retries,
                    "postgres_errors": self.postgres_errors,
                    "redis_errors": self.redis_errors,
                },
                "request_rate_per_second": round(rate, 4), "rate_limit_per_second": round(rate_limit, 2),
                "latency_ms": {"last": round(self.last_latency * 1000, 2), "average": round(self.total_latency / max(1, self.upstream_requests) * 1000, 2)},
                "endpoints": endpoints, "last_error_at": self.last_error_at,
            }
