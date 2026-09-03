import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.esi.client import EsiApiError, EsiClient
from app.esi.remote import EsiRequestMetrics, RemoteEsiClient
from scripts.esi_gateway import GatewayServer, GatewayState


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def test_remote_client_uses_gateway_and_records_cache_status():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"data": {"characters": [{"id": 42, "name": "Alice"}]}, "cache": "miss"})

    metrics = EsiRequestMetrics()
    client = RemoteEsiClient(
        "http://gateway.test",
        "x" * 32,
        opener=opener,
        metrics=metrics,
    )

    assert client.resolve_ids(["Alice"]) == {"characters": [{"id": 42, "name": "Alice"}]}
    request, timeout = requests[0]
    assert request.full_url == "http://gateway.test/v1/universe/ids"
    assert request.get_header("Authorization") == "Bearer " + "x" * 32
    assert timeout == 8.0
    assert metrics.snapshot()["counts"]["resolve_ids:miss:remote"] == 1


def test_remote_client_uses_bulk_affiliation_gateway_route():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(
            {
                "data": [{"character_id": 123, "corporation_id": 456}],
                "cache": "miss",
            }
        )

    client = RemoteEsiClient("http://gateway.test", "x" * 32, opener=opener)
    assert client.get_character_affiliations([123]) == [
        {"character_id": 123, "corporation_id": 456}
    ]
    assert requests[0].full_url == "http://gateway.test/v1/characters/affiliation"
    assert json.loads(requests[0].data.decode("utf-8")) == [123]


def test_remote_client_falls_back_to_local_client_on_gateway_error(monkeypatch):
    def opener(request, timeout):
        raise OSError("gateway offline")

    local = EsiClient(base_url="https://esi.test/latest")
    monkeypatch.setattr(local, "get_system", lambda system_id: {"name": "Jita", "system_id": system_id})
    client = RemoteEsiClient("http://gateway.test", "x" * 32, opener=opener, fallback=local)

    assert client.get_system(30000142) == {"name": "Jita", "system_id": 30000142}
    assert client.metrics.snapshot()["counts"]["get_system:local:fallback"] == 1


def test_remote_client_reads_gateway_health():
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse({"ok": True, "requests": 3, "cache_hits": 2})

    client = RemoteEsiClient("http://gateway.test", "x" * 32, opener=opener)
    assert client.gateway_health()["cache_hits"] == 2
    assert requests == ["http://gateway.test/health"]
    assert client.metrics.snapshot()["counts"]["gateway_health:none:remote"] == 1


def test_remote_client_without_fallback_raises_esi_error():
    def opener(request, timeout):
        raise OSError("gateway offline")

    client = RemoteEsiClient("http://gateway.test", "x" * 32, opener=opener)
    try:
        client.get_character(42)
    except EsiApiError as exc:
        assert "gateway offline" in str(exc)
    else:
        raise AssertionError("expected EsiApiError")


def test_gateway_authenticates_caches_and_serves_public_profile():
    token = "t" * 32
    state = GatewayState(token, {"127.0.0.1"}, ttl=60, max_requests_per_second=100)
    calls = []
    state.client.get_system = lambda system_id: calls.append(system_id) or {"name": "Jita"}
    server = GatewayServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/systems/30000142"
    try:
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request) as response:
            first = json.loads(response.read())
        with urlopen(request) as response:
            second = json.loads(response.read())
        assert first["data"] == {"name": "Jita"}
        assert first["cache"] == "miss"
        assert second["cache"] == "hit"
        assert calls == [30000142]

        with urlopen(Request(
            f"http://127.0.0.1:{server.server_port}/health",
            headers={"Authorization": f"Bearer {token}"},
        )) as response:
            health = json.loads(response.read())
        assert health["requests"] == 2
        assert health["total_requests"] == 2
        assert health["upstream_requests"] == 1
        assert health["cache_misses"] == 1
        assert health["cache_hits"] == 1
        assert health["endpoints"]["get_system"]["requests"] == 2
        assert health["endpoints"]["get_system"]["cache_hits"] == 1
        assert health["cache_hit_rate"] > 0

        try:
            urlopen(url)
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("expected gateway authorization failure")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_batch_cache_key_is_order_insensitive():
    token = "t" * 32
    state = GatewayState(token, {"127.0.0.1"}, ttl=60, max_requests_per_second=100)
    calls = []
    state.client.resolve_ids = lambda names: calls.append(list(names)) or {"characters": []}
    server = GatewayServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/universe/ids"
    try:
        for names in (["Alice", "Bob"], ["Bob", "Alice"]):
            request = Request(
                url,
                data=json.dumps(names).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request) as response:
                json.loads(response.read())
        assert calls == [["Alice", "Bob"]]
        assert state.health()["cache_hits"] == 1
        assert state.health()["cache_misses"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_health_excludes_expired_cache_entries():
    state = GatewayState("t" * 32, {"127.0.0.1"}, ttl=60, max_requests_per_second=100)
    state.cache["expired"] = (time.monotonic() - 1, {})
    state.cache["active"] = (time.monotonic() + 60, {})
    assert state.health()["cache_entries"] == 1


def test_legacy_gateway_negative_hit_with_stale_value_returns_stale():
    state = GatewayState("t" * 32, {"127.0.0.1"}, ttl=60, max_requests_per_second=100)
    now = time.monotonic()
    with state.lock:
        state.cache["stale"] = (now - 1, {"name": "Jita"})
        state.negative["stale"] = now + 30

    def loader():
        raise AssertionError("negative stale hit must not call upstream")

    value, status = state.fetch("stale", loader, endpoint="get_system")
    assert value == {"name": "Jita"}
    assert status == "stale"


def test_gateway_coalesces_concurrent_misses_per_key():
    state = GatewayState("t" * 32, {"127.0.0.1"}, ttl=60, max_requests_per_second=100)
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
    assert sorted(result[1] for result in results) == ["hit", "miss"]
    assert state.health()["coalesced"] == 1
