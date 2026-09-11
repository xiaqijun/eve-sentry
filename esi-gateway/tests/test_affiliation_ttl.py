"""A generic one-day cache must not hold up current personnel confirmation."""

import pytest

from esi_gateway.id_cache import CacheRecord, IdCacheCoordinator, MemoryStore
from esi_gateway.server import GatewayState, affiliation_cache_ttl


@pytest.mark.parametrize("configured,expected", [(300, 3600), (86400, 3600), (60, 3600), (3600, 3600)])
def test_affiliation_expires_independently_of_generic_cache(monkeypatch, configured, expected):
    clock = [1000.0]
    monkeypatch.setattr("esi_gateway.server.time.time", lambda: clock[0])
    monkeypatch.setattr("esi_gateway.server.time.monotonic", lambda: clock[0])
    state = GatewayState("t" * 32, set(), 86400, 1000, affiliation_ttl=configured)
    calls = []

    def loader():
        calls.append(1)
        return [{"character_id": 1, "corporation_id": 10}]

    metadata = {}
    state.fetch("affiliation", loader, endpoint="get_character_affiliations", freshness=metadata)
    state.fetch("name", lambda: "Pilot", endpoint="resolve_ids")
    assert metadata["expires_at"] - metadata["fetched_at"] == expected
    clock[0] += expected - 1
    assert state.fetch("affiliation", loader, endpoint="get_character_affiliations")[1] == "hit"
    clock[0] += 2
    assert state.fetch("affiliation", loader, endpoint="get_character_affiliations", freshness=metadata)[1] == "miss"
    assert metadata["fetched_at"] == clock[0]
    assert len(calls) == 2
    assert state.fetch("name", lambda: "Wrong", endpoint="resolve_ids") == ("Pilot", "hit")


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_affiliation_ttl_rejected(value):
    with pytest.raises(ValueError):
        affiliation_cache_ttl(value)


@pytest.mark.parametrize("with_hot", [False, True])
def test_durable_day_long_affiliation_is_revalidated(with_hot):
    import time

    now = time.time()
    durable, hot = MemoryStore(), MemoryStore() if with_hot else None
    record = CacheRecord("get_character_affiliations", "1", {"character_id": 1, "corporation_id": 99},
                         now - 7200, now + 79200, now + 79500)
    durable.put_many([record])
    if hot is not None:
        hot.put_many([record])
    coordinator = IdCacheCoordinator(durable, hot, ttl_seconds=86400)
    calls = []
    try:
        metadata = {}
        values, statuses = coordinator.fetch_batch(
            "get_character_affiliations", [1],
            lambda ids: calls.append(ids) or [{"character_id": 1, "corporation_id": 10}],
            lambda rows: {str(row["character_id"]): row for row in rows}, freshness=metadata)
        assert calls == [["1"]]
        assert statuses == {"1": "miss"}
        assert values["1"]["corporation_id"] == 10
        assert metadata["1"]["fetched_at"] >= now
        assert metadata["1"]["expires_at"] - metadata["1"]["fetched_at"] == 3600
    finally:
        coordinator.close()
