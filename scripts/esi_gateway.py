"""Private, allow-listed public ESI proxy with a small response cache."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/scripts/", 1)[0])

from app.esi.client import EsiApiError, EsiClient

logger = logging.getLogger("eve_sentry.esi_gateway")
MAX_BODY_BYTES = 64 * 1024
MAX_BATCH_ITEMS = 1000
ID_PATHS = {
    "characters": ("get_character", "/characters/{id}"),
    "corporations": ("get_corporation", "/corporations/{id}"),
    "alliances": ("get_alliance", "/alliances/{id}"),
    "systems": ("get_system", "/systems/{id}"),
}


class GatewayState:
    def __init__(self, token: str, allowed_clients: set[str], ttl: float, max_requests_per_second: float) -> None:
        self.token = token
        self.allowed_clients = allowed_clients
        self.ttl = max(1.0, ttl)
        self.min_interval = 1.0 / max(0.1, max_requests_per_second)
        self.client = EsiClient(timeout=10.0, user_agent="eve-sentry-esi-gateway/1.0")
        self.cache: dict[str, tuple[float, Any]] = {}
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.started_at = time.monotonic()
        self.last_request_at = 0.0
        self.requests = 0
        self.upstream_requests = 0
        self.cache_misses = 0
        self.cache_hits = 0
        self.errors = 0
        self.total_latency = 0.0
        self.last_latency = 0.0
        self.last_error_at = 0.0
        self._request_times: deque[float] = deque(maxlen=10000)
        self._upstream_request_times: deque[float] = deque(maxlen=10000)
        self._endpoint_metrics: dict[str, dict[str, Any]] = {}

    def _purge_expired_locked(self, now: float) -> None:
        expired = [key for key, (expires_at, _value) in self.cache.items() if expires_at <= now]
        for key in expired:
            self.cache.pop(key, None)

    def _endpoint_metric_locked(self, endpoint: str) -> dict[str, Any]:
        return self._endpoint_metrics.setdefault(
            endpoint,
            {
                "requests": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "upstream_requests": 0,
                "errors": 0,
                "total_latency": 0.0,
                "last_latency": 0.0,
            },
        )

    def fetch(self, key: str, loader, *, endpoint: str = "unknown"):
        now = time.monotonic()
        with self.lock:
            self._purge_expired_locked(now)
            self.requests += 1
            self._request_times.append(now)
            metric = self._endpoint_metric_locked(endpoint)
            metric["requests"] += 1
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                self.cache_hits += 1
                metric["cache_hits"] += 1
                return cached[1], "hit"
        with self.request_lock:
            now = time.monotonic()
            with self.lock:
                self._purge_expired_locked(now)
                cached = self.cache.get(key)
                if cached and cached[0] > now:
                    self.cache_hits += 1
                    self._endpoint_metric_locked(endpoint)["cache_hits"] += 1
                    return cached[1], "hit"
                self.cache_misses += 1
                metric = self._endpoint_metric_locked(endpoint)
                metric["cache_misses"] += 1
                self.upstream_requests += 1
                self._upstream_request_times.append(now)
                metric["upstream_requests"] += 1
            remaining = self.min_interval - (now - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
            self.last_request_at = time.monotonic()
            request_started = time.monotonic()
            try:
                value = loader()
            except Exception:
                with self.lock:
                    self.errors += 1
                    self._endpoint_metric_locked(endpoint)["errors"] += 1
                    self.last_error_at = time.time()
                raise
            duration = time.monotonic() - request_started
            with self.lock:
                self.cache[key] = (time.monotonic() + self.ttl, value)
                self.total_latency += duration
                self.last_latency = duration
                metric = self._endpoint_metric_locked(endpoint)
                metric["total_latency"] += duration
                metric["last_latency"] = duration
            return value, "miss"

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            self._purge_expired_locked(now)
            uptime = max(0.0, now - self.started_at)
            window = max(1.0, min(60.0, uptime))
            request_rate = sum(
                timestamp >= now - window for timestamp in self._request_times
            ) / window
            upstream_rate = sum(
                timestamp >= now - window for timestamp in self._upstream_request_times
            ) / window
            endpoint_metrics = {}
            for endpoint, metric in self._endpoint_metrics.items():
                endpoint_metrics[endpoint] = {
                    "requests": metric["requests"],
                    "cache_hits": metric["cache_hits"],
                    "cache_misses": metric["cache_misses"],
                    "upstream_requests": metric["upstream_requests"],
                    "errors": metric["errors"],
                    "cache_hit_rate": round(
                        metric["cache_hits"] / max(1, metric["requests"]), 4
                    ),
                    "last_latency_ms": round(metric["last_latency"] * 1000, 2),
                    "average_latency_ms": round(
                        metric["total_latency"]
                        / max(1, metric["upstream_requests"])
                        * 1000,
                        2,
                    ),
                }
            return {
                "ok": True,
                "service": "eve-sentry-esi-gateway",
                "version": "1.0",
                "uptime_seconds": round(uptime, 1),
                "requests": self.requests,
                "total_requests": self.requests,
                "upstream_requests": self.upstream_requests,
                "cache_misses": self.cache_misses,
                "errors": self.errors,
                "cache_hits": self.cache_hits,
                "cache_entries": len(self.cache),
                "cache_hit_rate": round(self.cache_hits / max(1, self.requests), 4),
                "request_rate_per_second": round(request_rate, 4),
                "upstream_rate_per_second": round(upstream_rate, 4),
                "rate_limit_per_second": round(1.0 / self.min_interval, 2),
                "latency_ms": {
                    "last": round(self.last_latency * 1000, 2),
                    "average": round(
                        self.total_latency / max(1, self.upstream_requests) * 1000,
                        2,
                    ),
                },
                "endpoints": endpoint_metrics,
                "last_error_at": self.last_error_at or None,
            }


class GatewayHandler(BaseHTTPRequestHandler):
    server: "GatewayServer"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(self.server.state.health())
            return
        if not self._authorized():
            return
        match = re.fullmatch(r"/v1/(characters|corporations|alliances|systems)/(\d+)/?", self.path)
        if not match:
            self._send_error(HTTPStatus.NOT_FOUND, "route_not_found")
            return
        kind, raw_id = match.groups()
        entity_id = int(raw_id)
        method_name, esi_path = ID_PATHS[kind]
        key = f"GET:{kind}:{entity_id}"
        try:
            data, cache = self.server.state.fetch(
                key,
                lambda: getattr(self.server.state.client, method_name)(entity_id),
                endpoint=method_name,
            )
        except (EsiApiError, ValueError):
            self._send_error(HTTPStatus.BAD_GATEWAY, "esi_unavailable")
            return
        self._send_json({"data": data, "cache": cache, "endpoint": esi_path.format(id=entity_id)})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path.rstrip("/") not in {"/v1/universe/ids", "/v1/universe/names"}:
            self._send_error(HTTPStatus.NOT_FOUND, "route_not_found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid_body_size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, list) or len(payload) > MAX_BATCH_ITEMS:
                raise ValueError("invalid_batch")
            if self.path.rstrip("/") == "/v1/universe/ids":
                if not all(isinstance(item, str) and item.strip() for item in payload):
                    raise ValueError("invalid_names")
                loader = lambda: self.server.state.client.resolve_ids(payload)
                canonical = sorted({item.strip() for item in payload}, key=str.casefold)
                key = "POST:ids:" + hashlib.sha256(json.dumps(canonical).encode()).hexdigest()
                endpoint = "resolve_ids"
            else:
                ids = [int(item) for item in payload]
                if any(item <= 0 for item in ids):
                    raise ValueError("invalid_ids")
                loader = lambda: self.server.state.client.resolve_names(ids)
                canonical = sorted(set(ids))
                key = "POST:names:" + hashlib.sha256(json.dumps(canonical).encode()).hexdigest()
                endpoint = "resolve_names"
            data, cache = self.server.state.fetch(key, loader, endpoint=endpoint)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_payload")
            return
        except EsiApiError:
            self._send_error(HTTPStatus.BAD_GATEWAY, "esi_unavailable")
            return
        self._send_json({"data": data, "cache": cache})

    def _authorized(self) -> bool:
        peer = self.client_address[0]
        if self.server.state.allowed_clients and peer not in self.server.state.allowed_clients:
            self._send_error(HTTPStatus.FORBIDDEN, "source_not_allowed")
            return False
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.state.token}"
        if not hmac.compare_digest(supplied, expected):
            self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return False
        return True

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, code: str) -> None:
        self._send_json({"error": code}, status)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("gateway request path=%s status=%s", self.path.split("?", 1)[0], args[1] if len(args) > 1 else "")


class GatewayServer(ThreadingHTTPServer):
    def __init__(self, address, state: GatewayState):
        super().__init__(address, GatewayHandler)
        self.state = state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the private ESI Gateway")
    parser.add_argument("--host", default=os.environ.get("EVE_SENTRY_ESI_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EVE_SENTRY_ESI_GATEWAY_PORT", "8787")))
    parser.add_argument("--token", default=os.environ.get("EVE_SENTRY_ESI_GATEWAY_TOKEN", ""))
    parser.add_argument("--allowed-client", action="append", default=None)
    parser.add_argument("--cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CACHE_TTL", "86400")))
    parser.add_argument("--rate", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_RATE", "2")))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = str(args.token or "").strip()
    if len(token) < 32:
        raise SystemExit("--token or EVE_SENTRY_ESI_GATEWAY_TOKEN must be at least 32 characters")
    allowed = set(args.allowed_client or os.environ.get("EVE_SENTRY_ESI_GATEWAY_ALLOWED_CLIENTS", "").replace(",", " ").split())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = GatewayServer((args.host, args.port), GatewayState(token, allowed, args.cache_ttl, args.rate))
    logger.info("ESI Gateway listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
