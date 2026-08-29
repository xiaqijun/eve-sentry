"""HTTP server for the private allow-listed public ESI gateway."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .auth import Authorizer
from .cache import TtlCache
from .client import EsiApiError, EsiClient
from .health import HealthMetrics
from .rate_limit import RateLimiter

MAX_BODY_BYTES = 64 * 1024
MAX_BATCH_ITEMS = 1000
ID_PATHS = {"characters": ("get_character", "/characters/{id}"), "corporations": ("get_corporation", "/corporations/{id}"), "alliances": ("get_alliance", "/alliances/{id}"), "systems": ("get_system", "/universe/systems/{id}")}


class GatewayState:
    def __init__(self, token: str, allowed_clients: set[str], ttl: float, max_requests_per_second: float, client: EsiClient | None = None) -> None:
        self.authorizer = Authorizer(token, allowed_clients)
        self.cache = TtlCache(ttl)
        self.rate_limiter = RateLimiter(max_requests_per_second)
        self.client = client or EsiClient(timeout=10.0)
        self.metrics = HealthMetrics()
        self._request_lock = threading.Lock()

    def fetch(self, key: str, loader: Callable[[], Any], *, endpoint: str) -> tuple[Any, str]:
        hit, value = self.cache.get(key)
        self.metrics.record_request(endpoint, cached=hit)
        if hit:
            self.metrics.cache_hits += 1
            return value, "hit"
        self.metrics.cache_misses += 1
        with self._request_lock:
            hit, value = self.cache.get(key)
            if hit:
                self.metrics.cache_hits += 1
                return value, "hit"
            self.rate_limiter.wait()
            started = time.monotonic()
            try:
                value = loader()
            except Exception:
                self.metrics.record_error(endpoint)
                raise
            duration = time.monotonic() - started
            self.cache.set(key, value)
            self.metrics.record_upstream(endpoint, duration)
            return value, "miss"

    def health(self) -> dict[str, Any]:
        return self.metrics.snapshot(self.cache.size(), self.rate_limiter.requests_per_second)


class GatewayHandler(BaseHTTPRequestHandler):
    server: "GatewayServer"

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/health":
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
        try:
            data, cache = self.server.state.fetch(f"GET:{kind}:{entity_id}", lambda: getattr(self.server.state.client, method_name)(entity_id), endpoint=method_name)
        except (EsiApiError, ValueError):
            self._send_error(HTTPStatus.BAD_GATEWAY, "esi_unavailable")
            return
        self._send_json({"data": data, "cache": cache, "endpoint": esi_path.format(id=entity_id)})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        route = self.path.split("?", 1)[0].rstrip("/")
        if route not in {"/v1/universe/ids", "/v1/universe/names"}:
            self._send_error(HTTPStatus.NOT_FOUND, "route_not_found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, list) or len(payload) > MAX_BATCH_ITEMS:
                raise ValueError
            if route.endswith("/ids"):
                if not all(isinstance(item, str) and item.strip() for item in payload):
                    raise ValueError
                loader = lambda: self.server.state.client.resolve_ids(payload)
                canonical = sorted({item.strip() for item in payload}, key=str.casefold)
                endpoint = "resolve_ids"
            else:
                ids = [int(item) for item in payload]
                if any(item <= 0 for item in ids):
                    raise ValueError
                loader = lambda: self.server.state.client.resolve_names(ids)
                canonical = sorted(set(ids))
                endpoint = "resolve_names"
            key = f"POST:{endpoint}:" + hashlib.sha256(json.dumps(canonical).encode()).hexdigest()
            data, cache = self.server.state.fetch(key, loader, endpoint=endpoint)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_payload")
            return
        except EsiApiError:
            self._send_error(HTTPStatus.BAD_GATEWAY, "esi_unavailable")
            return
        self._send_json({"data": data, "cache": cache})

    def _authorized(self) -> bool:
        code = self.server.state.authorizer.check(self.client_address[0], self.headers.get("Authorization", ""))
        if code:
            self._send_error(HTTPStatus.FORBIDDEN if code == "source_not_allowed" else HTTPStatus.UNAUTHORIZED, code)
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
        return


class GatewayServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: GatewayState) -> None:
        super().__init__(address, GatewayHandler)
        self.state = state
