import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from esi_gateway.auth import Authorizer
from esi_gateway.cache import TtlCache
from esi_gateway.client import EsiApiError, EsiClient
from esi_gateway.server import GatewayServer, GatewayState


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def test_public_client_uses_public_paths_and_maps_transport_errors():
    requests = []

    def opener(request, timeout):
        requests.append((request.full_url, request.method, timeout))
        return FakeResponse({"name": "Jita"})

    client = EsiClient(opener=opener)
    assert client.get_system(30000142) == {"name": "Jita"}
    assert requests == [("https://esi.evetech.net/latest/universe/systems/30000142/", "GET", 10.0)]

    def offline(*_args, **_kwargs):
        raise OSError("offline")

    with pytest.raises(EsiApiError, match="offline"):
        EsiClient(opener=offline).get_system(1)


def test_cache_and_auth_are_isolated_primitives():
    cache = TtlCache(60)
    assert cache.get("x") == (False, None)
    cache.set("x", {"ok": True})
    assert cache.get("x") == (True, {"ok": True})
    auth = Authorizer("t" * 32, {"127.0.0.1"})
    assert auth.check("127.0.0.1", "Bearer " + "t" * 32) is None
    assert auth.check("127.0.0.1", "") == "unauthorized"


def test_cache_evicts_oldest_entries_and_expired_entries():
    cache = TtlCache(60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == (True, 1)
    cache.set("c", 3)
    assert cache.get("b") == (False, None)
    assert cache.evictions == 1

    expired = TtlCache(1, max_entries=2)
    expired.set("x", 1)
    time.sleep(1.02)
    assert expired.get("x") == (False, None)
    assert expired.evictions == 1


def test_gateway_authenticates_caches_and_reports_health():
    token = "t" * 32
    state = GatewayState(token, {"127.0.0.1"}, 60, 100)
    calls = []
    state.client.get_system = lambda system_id: calls.append(system_id) or {"name": "Jita"}
    server = GatewayServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/systems/30000142"
    try:
        req = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(req) as response:
            first = json.loads(response.read())
        with urlopen(req) as response:
            second = json.loads(response.read())
        assert first["cache"] == "miss"
        assert second["cache"] == "hit"
        assert calls == [30000142]
        with urlopen(f"http://127.0.0.1:{server.server_port}/health") as response:
            health = json.loads(response.read())
        assert health["requests"] == 2
        assert health["cache_hits"] == 1
        assert health["upstream_requests"] == 1
        assert health["endpoints"]["get_system"]["requests"] == 2
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_batch_cache_key_is_order_insensitive():
    token = "t" * 32
    state = GatewayState(token, {"127.0.0.1"}, 60, 100)
    calls = []
    state.client.resolve_ids = lambda names: calls.append(list(names)) or {"characters": []}
    server = GatewayServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/universe/ids"
    try:
        for names in (["Alice", "Bob"], ["Bob", "Alice"]):
            req = Request(url, data=json.dumps(names).encode(), method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urlopen(req) as response:
                json.loads(response.read())
        assert calls == [["Alice", "Bob"]]
        assert state.health()["cache_hits"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_coalesces_concurrent_misses_per_key():
    token = "t" * 32
    state = GatewayState(token, {"127.0.0.1"}, 60, 100)
    calls = []

    def loader():
        calls.append(1)
        time.sleep(0.05)
        return {"name": "Jita"}

    results = []

    def run():
        results.append(state.fetch("same-key", loader, endpoint="get_system"))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert calls == [1]
    assert results == [({"name": "Jita"}, "miss"), ({"name": "Jita"}, "hit")]
    assert state.metrics.coalesced == 1


def test_gateway_negative_cache_avoids_repeated_upstream_failures():
    state = GatewayState("t" * 32, set(), 60, 100, negative_ttl=30)
    calls = []

    def loader():
        calls.append(1)
        raise EsiApiError("offline")

    with pytest.raises(EsiApiError, match="offline"):
        state.fetch("failure", loader, endpoint="get_system")
    with pytest.raises(EsiApiError, match="cached_upstream_error"):
        state.fetch("failure", loader, endpoint="get_system")
    assert calls == [1]
    assert state.health()["negative_hits"] == 1


def test_gateway_serves_recent_stale_value_when_esi_fails():
    state = GatewayState("t" * 32, set(), 60, 100, stale_grace=30)
    state.cache._items["stale"] = (time.monotonic() - 1, {"name": "Jita"})

    def loader():
        raise EsiApiError("offline")

    value, status = state.fetch("stale", loader, endpoint="get_system")
    assert value == {"name": "Jita"}
    assert status == "stale"
    value, status = state.fetch("stale", loader, endpoint="get_system")
    assert value == {"name": "Jita"}
    assert status == "stale"
    assert state.health()["stale_served"] == 2
