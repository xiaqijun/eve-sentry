"""Authenticated fixed-destination TLS relay; never inspect ESI plaintext."""

from __future__ import annotations

import ipaddress
import selectors
import socket
import threading
import time
from collections import Counter
from http import HTTPStatus
from http.server import ThreadingHTTPServer

from .server import GatewayHandler

TARGET = ("esi.evetech.net", 443)
AUTHORITY = "esi.evetech.net:443"


def validate_relay_binding(host, allowed_clients):
    """A relay requires an explicit private bind address and exact source IPs."""
    address = ipaddress.ip_address(host)
    if address.is_unspecified or not address.is_private or not allowed_clients:
        raise ValueError("relay requires a private bind address and source allowlist")
    for peer in allowed_clients:
        if not ipaddress.ip_address(peer).is_private:
            raise ValueError("relay sources must be private IP addresses")


def copy_encrypted(client, upstream, *, idle_seconds=30, lifetime_seconds=300,
                   max_bytes=64 * 1024 * 1024):
    """Bound memory, connection lifetime and ciphertext volume in both directions."""
    started = last_progress = time.monotonic()
    total = 0
    peers = {client: upstream, upstream: client}
    buffers = {client: bytearray(), upstream: bytearray()}
    with selectors.DefaultSelector() as selector:
        for sock in peers:
            sock.setblocking(False)
            selector.register(sock, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            remaining = min(lifetime_seconds - (now - started), idle_seconds - (now - last_progress))
            if remaining <= 0:
                return
            for key, mask in selector.select(min(remaining, 1)):
                sock = key.fileobj
                if mask & selectors.EVENT_READ:
                    try:
                        data = sock.recv(16384)
                    except BlockingIOError:
                        data = None
                    if data == b"":
                        # The upstream may close immediately after its final TLS
                        # record. Drain bytes already received before closing.
                        destination = peers[sock]
                        if buffers[destination]:
                            destination.settimeout(max(0.001, remaining))
                            destination.sendall(buffers[destination])
                        return
                    if data:
                        total += len(data)
                        if total > max_bytes:
                            return
                        buffers[peers[sock]].extend(data)
                        last_progress = time.monotonic()
                if mask & selectors.EVENT_WRITE and buffers[sock]:
                    try:
                        sent = sock.send(buffers[sock])
                    except BlockingIOError:
                        sent = 0
                    if sent:
                        del buffers[sock][:sent]
                        last_progress = time.monotonic()
            for sock in peers:
                # At most ~80 KiB buffered per direction; backpressure reads.
                events = (selectors.EVENT_READ if len(buffers[peers[sock]]) < 65536 else 0)
                if buffers[sock]:
                    events |= selectors.EVENT_WRITE
                try:
                    selector.get_key(sock)
                except KeyError:
                    if events:
                        selector.register(sock, events)
                else:
                    if events:
                        selector.modify(sock, events)
                    else:
                        selector.unregister(sock)


class RelayHandler(GatewayHandler):
    # CONNECT clients wait for 200 before sending TLS. Do not prefetch ciphertext.
    rbufsize = 0

    def do_CONNECT(self):
        self.close_connection = True
        self.server.record_relay("requests")
        if not self._authorized():
            self.server.record_relay("rejected")
            return
        if (self.path != AUTHORITY or self.headers.get("Transfer-Encoding")
                or self.headers.get("Content-Length", "0") != "0"):
            self._send_error(HTTPStatus.BAD_REQUEST, "fixed_target_required")
            self.server.record_relay("rejected")
            return
        if not self.server.tunnels.acquire(blocking=False):
            self.server.record_relay("busy")
            self._send_json({"error": "relay_busy"}, HTTPStatus.SERVICE_UNAVAILABLE, retry_after=5)
            return
        try:
            self.server.record_relay("attempts")
            self.server.record_relay("active", 1)
            try:
                upstream = socket.create_connection(TARGET, timeout=10)
            except OSError:
                self.server.record_relay("connect_errors")
                self._send_error(HTTPStatus.BAD_GATEWAY, "relay_connect_failed")
                return
            with upstream:
                self.server.record_relay("connected")
                self.send_response(HTTPStatus.OK)
                self.end_headers()
                self.wfile.flush()
                try:
                    copy_encrypted(self.connection, upstream)
                except OSError:
                    self.server.record_relay("stream_errors")
        finally:
            self.server.record_relay("active", -1)
            self.server.tunnels.release()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send_json({**self.server.state.health(), "transport_mode": self.server.transport_mode,
                             "relay": self.server.relay_metrics()})
        elif self.server.transport_mode == "relay":
            self._send_error(HTTPStatus.GONE, "json_gateway_disabled")
        else:
            super().do_GET()

    def do_POST(self):
        if self.server.transport_mode == "relay":
            self._send_error(HTTPStatus.GONE, "json_gateway_disabled")
        else:
            super().do_POST()


class RelayGatewayServer(ThreadingHTTPServer):
    """Legacy public routes can coexist during migration; all workers are bounded."""

    daemon_threads = True

    def __init__(self, address, state, *, transport_mode="dual", max_tunnels=8):
        validate_relay_binding(address[0], state.authorizer.allowed_clients)
        if transport_mode not in {"dual", "relay"} or not 1 <= max_tunnels <= 16:
            raise ValueError("invalid relay limits or mode")
        self.state, self.transport_mode = state, transport_mode
        self.metrics_lock = threading.Lock()
        self.metrics = Counter()
        self.tunnels = threading.BoundedSemaphore(max_tunnels)
        self.workers = threading.BoundedSemaphore(max_tunnels + 8)
        super().__init__(address, RelayHandler)

    def record_relay(self, key, count=1):
        with self.metrics_lock:
            self.metrics[key] += count

    def relay_metrics(self):
        with self.metrics_lock:
            return dict(self.metrics)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(10)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()
