import json
import threading
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
        assert health["requests"] == 1
        assert health["cache_hits"] == 1
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
