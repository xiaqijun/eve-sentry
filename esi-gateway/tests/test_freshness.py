"""Freshness must describe the cached payload, not the time of its delivery."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

import pytest

from esi_gateway.client import EsiApiError
from esi_gateway.id_cache import IdCacheCoordinator, MemoryStore
from esi_gateway.server import GatewayServer, GatewayState


@pytest.mark.parametrize("id_cache", [False, True])
@pytest.mark.parametrize("path,body,method,data,keys", [
    ("characters/1", None, "get_character", {"name": "Pilot"}, {"1"}),
    ("corporations/10", None, "get_corporation", {"name": "Corp"}, {"10"}),
    ("alliances/20", None, "get_alliance", {"name": "Alliance"}, {"20"}),
    ("systems/30", None, "get_system", {"name": "System"}, {"30"}),
    ("universe/ids", ["Pilot", "Missing"], "resolve_ids",
     {"characters": [{"id": 1, "name": "Pilot"}]}, {"pilot"}),
    ("universe/names", [1, 2], "resolve_names",
     [{"id": 1, "name": "Pilot", "category": "character"}], {"1"}),
    ("characters/affiliation", [1, 2], "get_character_affiliations",
     [{"character_id": 1, "corporation_id": 10}], {"1"}),
])
def test_all_routes_preserve_acquisition_time_on_cache_hit(id_cache, path, body, method, data, keys):
    coordinator = IdCacheCoordinator(MemoryStore(), refresh_interval_seconds=60) if id_cache else None
    state = GatewayState("t" * 32, {"127.0.0.1"}, 60, 1000, id_cache=coordinator)
    setattr(state.client, method, lambda *args: data)
    server = GatewayServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers = {"Authorization": "Bearer " + "t" * 32, "Content-Type": "application/json"}
        # Missing entities have no data and must not receive invented freshness.
        def request(payload):
            req = Request(f"http://127.0.0.1:{server.server_port}/v1/{path}",
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers)
            with urlopen(req, timeout=3) as response:
                return json.load(response)
        first = request(body)
        second = request(body)
        assert first["data"] == second["data"] == data
        assert set(first["freshness"]) == keys
        assert first["freshness"] == second["freshness"]
        for metadata in first["freshness"].values():
            assert metadata["fetched_at"] > 0
            assert metadata["last_validated_at"] == metadata["fetched_at"]
            assert metadata["expires_at"] > metadata["fetched_at"]
            assert metadata["stale"] is False
        if not id_cache or body is None:
            assert second["cache"] == "hit"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        state.close()


def test_stale_and_negative_cache_keep_original_time_then_refresh(monkeypatch):
    clock = {"mono": 100.0, "wall": 1000.0}
    monkeypatch.setattr("esi_gateway.server.time.monotonic", lambda: clock["mono"])
    monkeypatch.setattr("esi_gateway.server.time.time", lambda: clock["wall"])
    state = GatewayState("t" * 32, set(), 60, 1000, stale_grace=30, negative_ttl=10)
    metadata = {}
    assert state.fetch("a", lambda: {"name": "Pilot"}, endpoint="get_character", freshness=metadata)[1] == "miss"
    original = dict(metadata)
    clock.update(mono=161.0, wall=1061.0)
    calls = []

    def offline():
        calls.append(1)
        raise EsiApiError("offline")

    for _ in range(2):
        assert state.fetch("a", offline, endpoint="get_character", freshness=metadata)[1] == "stale"
        assert metadata == {**original, "stale": True}
    assert len(calls) == 1  # Negative-cache fallback also preserves the old age.
    clock.update(mono=172.0, wall=1072.0)
    state.fetch("a", lambda: {"name": "Pilot"}, endpoint="get_character", freshness=metadata)
    assert metadata["fetched_at"] == 1072.0
    assert metadata["expires_at"] == 1132.0
    assert metadata["stale"] is False


def test_coalesced_readers_share_payload_timestamp():
    state = GatewayState("t" * 32, set(), 60, 1000)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def loader():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"name": "Pilot"}

    def read():
        metadata = {}
        data, _ = state.fetch("a", loader, endpoint="get_character", freshness=metadata)
        return data, metadata

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(read)
        assert entered.wait(3)
        others = [pool.submit(read) for _ in range(3)]
        release.set()
        expected = first.result(timeout=3)
        assert all(future.result(timeout=3) == expected for future in others)
    assert len(calls) == 1


def test_unknown_legacy_entry_does_not_inherit_previous_metadata():
    state = GatewayState("t" * 32, set(), 60, 1000)
    state.cache.set("old", {"name": "Pilot"})
    metadata = {"fetched_at": 1234}
    assert state.fetch("old", lambda: None, endpoint="get_character", freshness=metadata) == ({"name": "Pilot"}, "hit")
    assert metadata == {}
