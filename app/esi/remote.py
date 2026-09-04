"""Remote public ESI client with metrics and local fallback."""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter, deque
from threading import Lock
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.esi.client import EsiApiError, EsiClient


class EsiRequestMetrics:
    """Thread-safe counters for remote ESI calls."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: Counter[str] = Counter()
        self._durations: dict[str, list[float]] = {}
        self._endpoint_stats: dict[str, dict[str, Any]] = {}
        self._request_times: deque[float] = deque(maxlen=10000)

    def record(self, endpoint: str, duration: float, *, cache: str, fallback: bool) -> None:
        key = f"{endpoint}:{cache}:{'fallback' if fallback else 'remote'}"
        now = time.monotonic()
        with self._lock:
            self._counts[key] += 1
            self._durations.setdefault(endpoint, []).append(max(0.0, duration))
            if len(self._durations[endpoint]) > 1000:
                self._durations[endpoint] = self._durations[endpoint][-1000:]
            stats = self._endpoint_stats.setdefault(
                endpoint,
                {
                    "requests": 0,
                    "remote_requests": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "fallback_requests": 0,
                    "errors": 0,
                    "total_duration": 0.0,
                    "last_duration": 0.0,
                },
            )
            stats["requests"] += 1
            stats["total_duration"] += max(0.0, duration)
            stats["last_duration"] = max(0.0, duration)
            if fallback:
                stats["fallback_requests"] += 1
            else:
                stats["remote_requests"] += 1
                if cache == "hit":
                    stats["cache_hits"] += 1
                elif cache == "miss":
                    stats["cache_misses"] += 1
                elif cache == "error":
                    stats["errors"] += 1
            self._request_times.append(now)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            window = 60.0
            request_rate = sum(
                timestamp >= now - window for timestamp in self._request_times
            ) / window
            endpoints = {
                endpoint: {
                    "requests": stats["requests"],
                    "remote_requests": stats["remote_requests"],
                    "cache_hits": stats["cache_hits"],
                    "cache_misses": stats["cache_misses"],
                    "fallback_requests": stats["fallback_requests"],
                    "errors": stats["errors"],
                    "cache_hit_rate": round(
                        stats["cache_hits"] / max(1, stats["remote_requests"]),
                        4,
                    ),
                    "last_ms": round(stats["last_duration"] * 1000, 2),
                    "average_ms": round(
                        stats["total_duration"] / max(1, stats["requests"]) * 1000,
                        2,
                    ),
                    "p50_ms": round(_percentile(self._durations.get(endpoint, []), 0.50) * 1000, 2),
                    "p95_ms": round(_percentile(self._durations.get(endpoint, []), 0.95) * 1000, 2),
                }
                for endpoint, stats in self._endpoint_stats.items()
            }
            return {
                "counts": dict(self._counts),
                "totals": {
                    "requests": sum(self._counts.values()),
                    "remote_requests": sum(
                        stats["remote_requests"] for stats in self._endpoint_stats.values()
                    ),
                    "cache_hits": sum(
                        stats["cache_hits"] for stats in self._endpoint_stats.values()
                    ),
                    "cache_misses": sum(
                        stats["cache_misses"] for stats in self._endpoint_stats.values()
                    ),
                    "fallback_requests": sum(
                        stats["fallback_requests"] for stats in self._endpoint_stats.values()
                    ),
                    "errors": sum(
                        stats["errors"] for stats in self._endpoint_stats.values()
                    ),
                    "request_rate_per_second": round(request_rate, 4),
                },
                "endpoints": endpoints,
                "durations_ms": {
                    endpoint: {
                        "count": len(values),
                        "last": round(values[-1] * 1000, 2),
                        "p50": round(_percentile(values, 0.50) * 1000, 2),
                        "p95": round(_percentile(values, 0.95) * 1000, 2),
                    }
                    for endpoint, values in self._durations.items()
                    if values
                },
            }


class RemoteEsiClient(EsiClient):
    """Use a private ESI Gateway for public calls and optionally fall back locally."""

    def __init__(
        self,
        gateway_url: str,
        gateway_token: str,
        *,
        timeout: float = 8.0,
        fallback: EsiClient | None = None,
        opener: Callable[..., Any] | None = None,
        metrics: EsiRequestMetrics | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.gateway_url = str(gateway_url or "").rstrip("/")
        self.gateway_token = str(gateway_token or "").strip()
        if not self.gateway_url or not self.gateway_token:
            raise ValueError("gateway_url and gateway_token are required")
        self._remote_opener = opener or urlopen
        self.fallback = fallback
        self.metrics = metrics or EsiRequestMetrics()

    def resolve_ids(self, names: list[str]) -> dict[str, Any]:
        return self._public_call("POST", "/v1/universe/ids", names, "resolve_ids", dict)

    def resolve_names(self, ids: list[int]) -> list[dict[str, Any]]:
        payload = self._public_call("POST", "/v1/universe/names", ids, "resolve_names", list)
        if not isinstance(payload, list):
            raise EsiApiError("ESI Gateway returned invalid names payload")
        return payload

    def gateway_health(self) -> dict[str, Any]:
        """Read the private gateway health document without exposing its token."""
        started = time.monotonic()
        request = Request(
            f"{self.gateway_url}/health",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.gateway_token}",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with self._remote_opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise EsiApiError(self._read_error_message(exc)) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise EsiApiError(str(getattr(exc, "reason", exc))) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EsiApiError("ESI Gateway returned invalid health JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise EsiApiError("ESI Gateway health check failed")
        self.metrics.record("gateway_health", time.monotonic() - started, cache="none", fallback=False)
        return payload

    def get_character(self, character_id: int) -> dict[str, Any]:
        return self._public_call("GET", f"/v1/characters/{int(character_id)}", None, "get_character", dict)

    def get_character_affiliations(self, character_ids: list[int]) -> list[dict[str, Any]]:
        payload = self._public_call(
            "POST",
            "/v1/characters/affiliation",
            [int(character_id) for character_id in character_ids],
            "get_character_affiliations",
            list,
        )
        if not isinstance(payload, list):
            raise EsiApiError("ESI Gateway returned invalid affiliation payload")
        return payload

    def get_corporation(self, corporation_id: int) -> dict[str, Any]:
        return self._public_call("GET", f"/v1/corporations/{int(corporation_id)}", None, "get_corporation", dict)

    def get_alliance(self, alliance_id: int) -> dict[str, Any]:
        return self._public_call("GET", f"/v1/alliances/{int(alliance_id)}", None, "get_alliance", dict)

    def get_system(self, system_id: int) -> dict[str, Any]:
        return self._public_call("GET", f"/v1/systems/{int(system_id)}", None, "get_system", dict)

    def _public_call(
        self,
        method: str,
        path: str,
        payload: Any,
        endpoint: str,
        expected_type: type,
    ) -> Any:
        started = time.monotonic()
        try:
            result, cache_status = self._request_gateway(method, path, payload)
            if not isinstance(result, expected_type):
                raise EsiApiError("ESI Gateway returned an invalid payload")
            self.metrics.record(endpoint, time.monotonic() - started, cache=cache_status, fallback=False)
            return result
        except (EsiApiError, OSError, TimeoutError) as exc:
            if self.fallback is None:
                self.metrics.record(
                    endpoint,
                    time.monotonic() - started,
                    cache="error",
                    fallback=False,
                )
                if isinstance(exc, EsiApiError):
                    raise
                raise EsiApiError(str(exc)) from exc
            if payload is None:
                identifier = int(path.rsplit("/", 1)[-1])
                result = getattr(self.fallback, endpoint)(identifier)
            else:
                result = getattr(self.fallback, endpoint)(payload)
            self.metrics.record(endpoint, time.monotonic() - started, cache="local", fallback=True)
            return result

    def _request_gateway(self, method: str, path: str, payload: Any) -> tuple[Any, str]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.gateway_token}",
            "User-Agent": self.user_agent,
            "X-Request-ID": uuid.uuid4().hex,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.gateway_url}{path}", data=data, headers=headers, method=method)
        try:
            with self._remote_opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise EsiApiError(self._read_error_message(exc)) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise EsiApiError(str(getattr(exc, "reason", exc))) from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EsiApiError("ESI Gateway returned invalid JSON") from exc
        if not isinstance(envelope, dict) or "data" not in envelope:
            raise EsiApiError(str(envelope.get("error") if isinstance(envelope, dict) else "invalid gateway response"))
        return envelope["data"], str(envelope.get("cache") or "miss")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]
