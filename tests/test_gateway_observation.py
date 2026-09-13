"""Relay health is bounded, distinct from ESI traffic, and admin-only."""

import json
from types import SimpleNamespace

import pytest

from app.esi.gateway_observation import transport_gateway
from app.esi.transport import EsiConnections, TransportEsiClient
from app.server.http_server import IntelRequestHandler
from tests.test_esi_transport import Connection, Response


def client():
    return TransportEsiClient(
        EsiConnections(relay_host="10.1.2.3", relay_token="secret" * 8)
    )


def health(**extra):
    return {
        "ok": True,
        "service": "eve-sentry-esi-gateway",
        "transport_mode": "relay",
        "relay": {"connected": 632, "active": 2},
        **extra,
    }


def test_relay_probe_uses_fixed_private_address_without_auth_proxy_or_esi_permit(
    monkeypatch,
):
    connection = Connection(
        [Response(json.dumps(health(token="must-not-leak")).encode())]
    )
    created = []
    monkeypatch.setattr(
        "app.esi.gateway_observation.http.client.HTTPConnection",
        lambda *args, **kwargs: created.append((args, kwargs)) or connection,
    )
    transport = client()
    result = transport_gateway(transport)
    assert created == [(("10.1.2.3", 8787), {"timeout": 2})]
    assert connection.requests == [(("GET", "/health"), {})]
    assert connection.closed
    assert transport.connections.pool.qsize() == 6
    assert result["configured"] and result["reachable"]
    assert result["health"]["relay"] == {"connected": 632, "active": 2}
    assert "must-not-leak" not in str(result) and "secret" not in str(result)


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid",
        b"x" * 65537,
        b"[]",
        json.dumps(health(ok=False)).encode(),
        json.dumps(health(transport_mode=[])).encode(),
        json.dumps(health(relay=[])).encode(),
    ],
    ids=[
        "malformed",
        "oversized",
        "array",
        "unhealthy",
        "invalid-mode",
        "invalid-counters",
    ],
)
def test_invalid_or_oversized_health_does_not_claim_reachable(monkeypatch, payload):
    connection = Connection([Response(payload)])
    monkeypatch.setattr(
        "app.esi.gateway_observation.http.client.HTTPConnection",
        lambda *_a, **_k: connection,
    )
    result = transport_gateway(client())
    assert result["configured"] and not result["reachable"]
    assert result["transport_mode"] == "relay"
    assert "health" not in result and connection.closed


def test_timeout_is_sanitized_and_connection_closed(monkeypatch):
    connection = Connection([])

    def fail():
        raise TimeoutError("http://private/?token=secret")

    monkeypatch.setattr(connection, "getresponse", fail)
    monkeypatch.setattr(
        "app.esi.gateway_observation.http.client.HTTPConnection",
        lambda *_a, **_k: connection,
    )
    result = transport_gateway(client())
    assert not result["reachable"] and connection.closed
    assert "TimeoutError" in result["error"] and "secret" not in result["error"]


def test_direct_does_not_probe_and_legacy_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        "app.esi.gateway_observation.http.client.HTTPConnection",
        lambda *_a, **_k: pytest.fail("must not probe"),
    )
    assert transport_gateway(TransportEsiClient(EsiConnections())) == {
        "configured": False,
        "reachable": False,
        "transport_mode": "direct",
    }
    assert transport_gateway(object()) is None


def test_admin_snapshot_recognizes_relay_without_legacy_gateway_health(monkeypatch):
    connection = Connection([Response(json.dumps(health()).encode())])
    monkeypatch.setattr(
        "app.esi.gateway_observation.http.client.HTTPConnection",
        lambda *_a, **_k: connection,
    )
    handler = SimpleNamespace(
        _esi_config=dict,
        _esi_public_resolver=lambda: SimpleNamespace(client=client()),
        _store=lambda: object(),
        _public_esi_health=dict,
    )
    result = IntelRequestHandler._esi_gateway_observability(handler)
    assert result["gateway"]["transport_mode"] == "relay"
    assert result["gateway"]["configured"] and result["gateway"]["reachable"]
