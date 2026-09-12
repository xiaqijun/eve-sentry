"""Fixed-target relay boundary and ciphertext forwarding regressions."""

import http.client
import socket
import threading

import pytest

from esi_gateway.relay import RelayGatewayServer, copy_encrypted, validate_relay_binding
from esi_gateway.server import GatewayState


@pytest.fixture
def server():
    state = GatewayState("t" * 32, {"127.0.0.1"}, 60, 100)
    instance = RelayGatewayServer(("127.0.0.1", 0), state, transport_mode="relay", max_tunnels=1)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    worker.join(2)


@pytest.mark.parametrize("host,allowed", [("0.0.0.0", {"10.1.1.1"}), ("127.0.0.1", set()), ("8.8.8.8", {"10.1.1.1"}), ("127.0.0.1", {"8.8.8.8"})])
def test_public_or_unrestricted_binding_rejected(host, allowed):
    with pytest.raises(ValueError):
        validate_relay_binding(host, allowed)


@pytest.mark.parametrize("target,token,status", [("evil.test:443", "t" * 32, 400), ("esi.evetech.net:80", "t" * 32, 400), ("esi.evetech.net:443", "wrong", 401)])
def test_rejects_target_and_identity_without_network(server, monkeypatch, target, token, status):
    # connect client before patching the shared socket module.
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    client.connect()
    def forbidden(*args, **kwargs):
        pytest.fail("invalid relay request attempted upstream connection")
    monkeypatch.setattr("esi_gateway.relay.socket.create_connection", forbidden)
    client.request("CONNECT", target, headers={"Authorization": "Bearer " + token})
    response = client.getresponse()
    assert response.status == status
    response.read()
    client.close()


def test_ciphertext_moves_both_directions_and_eof_closes_worker():
    left, relay_left = socket.socketpair()
    relay_right, right = socket.socketpair()
    for sock in (left, right):
        sock.settimeout(2)
    worker = threading.Thread(target=copy_encrypted, args=(relay_left, relay_right), daemon=True)
    worker.start()
    try:
        left.sendall(b"opaque-client-tls")
        assert right.recv(1024) == b"opaque-client-tls"
        right.sendall(b"opaque-server-tls")
        assert left.recv(1024) == b"opaque-server-tls"
        left.close()
        worker.join(2)
        assert not worker.is_alive()
    finally:
        for sock in (left, right, relay_left, relay_right):
            sock.close()


def test_relay_only_disables_json_cache_routes_but_keeps_health(server):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    for path, status in (("/v1/characters/1", 410), ("/health", 200)):
        client.request("GET", path)
        response = client.getresponse()
        assert response.status == status
        response.read()
    assert server.state.cache.size() == 0
    client.close()


def test_busy_tunnel_is_rejected_before_opening_upstream(server, monkeypatch):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    client.connect()
    assert server.tunnels.acquire(blocking=False)
    def forbidden(*args, **kwargs):
        pytest.fail("full relay attempted upstream connection")
    monkeypatch.setattr("esi_gateway.relay.socket.create_connection", forbidden)
    try:
        client.request("CONNECT", "esi.evetech.net:443", headers={"Authorization": "Bearer " + "t" * 32})
        response = client.getresponse()
        assert response.status == 503
        assert response.headers["Retry-After"] == "5"
        response.read()
    finally:
        server.tunnels.release()
        client.close()
