"""Bounded, certificate-verified ESI connections on the state-owning server."""

from __future__ import annotations

import http.client
import io
import ipaddress
import math
import queue
import ssl
import threading
import time
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from app.esi.client import EsiClient
from app.esi.transport_metrics import TransportMetrics, observe_transport

HOST = "esi.evetech.net"
MAX_RESPONSE = 8 * 1024 * 1024


class BufferedResponse(io.BytesIO):
    def __init__(self, body, headers, status):
        super().__init__(body)
        self.headers, self.status = headers, status


class EsiConnections:
    """One process-wide budget for public/private ESI; no retries or exit switching."""

    def __init__(self, *, relay_host=None, relay_port=8787, relay_token="", size=6, rate=2):
        if type(size) is not int or not 1 <= size <= 8 or not math.isfinite(rate) or rate <= 0:
            raise ValueError("invalid ESI connection budget")
        if not 1 <= int(relay_port) <= 65535:
            raise ValueError("invalid relay port")
        if relay_host:
            address = ipaddress.ip_address(relay_host)
            if not address.is_private or address.is_unspecified or len(relay_token) < 32:
                raise ValueError("relay requires a private address and gateway credential")
        if any(char in relay_token for char in "\r\n"):
            raise ValueError("invalid relay credential")
        self.relay_host, self.relay_port, self.relay_token = relay_host, int(relay_port), relay_token
        self.context = ssl.create_default_context()
        self.pool = queue.LifoQueue(size)
        for _ in range(size):
            self.pool.put(None)
        self.lock = threading.Lock()
        self.next_request, self.blocked_until = 0.0, 0.0
        self.interval = 1 / rate
        self.local = threading.local()
        self.telemetry = TransportMetrics()

    def _connection(self, timeout):
        connection = http.client.HTTPSConnection(self.relay_host or HOST,
                                                self.relay_port if self.relay_host else 443,
                                                timeout=timeout, context=self.context)
        if self.relay_host:
            connection.set_tunnel(HOST, 443, headers={"Authorization": f"Bearer {self.relay_token}"})
        return connection

    def _permit(self, deadline):
        while True:
            with self.lock:
                now = time.monotonic()
                if now >= deadline:
                    raise URLError("ESI request budget unavailable")
                wait = max(self.next_request, self.blocked_until) - now
                if wait <= 0:
                    self.next_request = now + self.interval
                    return
            if now + wait >= deadline:
                raise URLError("ESI request budget unavailable")
            time.sleep(min(wait, 0.1))

    def _throttle(self, headers):
        raw = headers.get("Retry-After", "300")
        try:
            try:
                delay = float(raw)
            except ValueError:
                delay = parsedate_to_datetime(raw).timestamp() - time.time()
            if not 0 <= delay < float("inf"):
                delay = 300
        except (TypeError, ValueError, OverflowError):
            delay = 300
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + max(300, delay))

    @observe_transport
    def __call__(self, request, *, timeout):
        parsed = urlsplit(request.full_url)
        if (parsed.scheme != "https" or parsed.hostname != HOST or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise URLError("ESI destination not allowed")
        self.local.metadata = {}
        deadline = time.monotonic() + timeout
        try:
            entry = self.pool.get(timeout=max(0.001, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise URLError("ESI connection budget unavailable") from exc
        connection, reusable = entry[0] if entry else None, False
        try:
            # Check the shared cooldown after waiting for a connection. A permit
            # issued before queueing could otherwise escape a newer 429 pause.
            self._permit(deadline)
            if entry:
                connection, used_at = entry
                if time.monotonic() - used_at >= 20:
                    connection.close()
                    connection = None
            remaining = max(0.001, deadline - time.monotonic())
            connection = connection or self._connection(remaining)
            connection.timeout = remaining
            if connection.sock:
                connection.sock.settimeout(remaining)
            started = time.time()
            headers = dict(request.header_items())
            # Gateway credentials only occur in CONNECT, never in the ESI request.
            self.local.attempted = True
            connection.request(request.get_method(), parsed.path + ("?" + parsed.query if parsed.query else ""),
                               body=request.data, headers=headers)
            response = connection.getresponse()
            if response.status in {420, 429}:
                self._throttle(response.headers)
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ESI response deadline exceeded")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                if not chunk:
                    if getattr(response, "length", None) not in (None, 0):
                        raise OSError("incomplete ESI response")
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise OSError("ESI response too large")
            body = b"".join(chunks)
            reusable = not response.will_close
            received = time.time()
            self.local.metadata = {"headers": response.headers, "started": started, "received": received,
                                   "path": parsed.path, "method": request.get_method()}
            if response.status >= 300:
                # Preserve status and headers for conditional requests and Retry-After.
                raise HTTPError(request.full_url, response.status, "ESI HTTP error", response.headers, io.BytesIO(body))
            return BufferedResponse(body, response.headers, response.status)
        except URLError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise URLError("ESI transport failed") from exc
        finally:
            if connection and not reusable:
                connection.close()
                connection = None
            self.pool.put((connection, time.monotonic()) if connection else None)


class TransportEsiClient(EsiClient):
    def __init__(self, connections, **kwargs):
        super().__init__(**kwargs)
        self.connections = connections

    def _open(self, request, *, timeout):
        return self.connections(request, timeout=timeout)

    def response_freshness(self):
        from app.esi.http_freshness import public_freshness

        metadata = getattr(self.connections.local, "metadata", {})
        return {"_direct": public_freshness(metadata)} if metadata else {}


@lru_cache(maxsize=4)
def configured_connections(mode, relay_url, relay_token):
    if mode == "direct":
        return EsiConnections()
    parsed = urlsplit(relay_url)
    if (mode != "relay" or parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("invalid fixed-target ESI relay configuration")
    return EsiConnections(relay_host=parsed.hostname, relay_port=parsed.port or 8787, relay_token=relay_token)


def configured_client(args):
    """Legacy is the rollback default; explicit relay never falls back on errors."""
    import os

    mode = os.environ.get("EVE_SENTRY_ESI_TRANSPORT", "legacy").strip()
    if mode == "legacy":
        return None
    if mode not in {"direct", "relay"}:
        raise ValueError("ESI transport must be legacy, direct or relay")
    connections = configured_connections(mode, getattr(args, "esi_gateway_url", ""),
                                         getattr(args, "esi_gateway_token", ""))
    return TransportEsiClient(connections)
