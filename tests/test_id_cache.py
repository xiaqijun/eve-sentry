import time

from esi_gateway.id_cache import CacheRecord, IdCacheCoordinator, MemoryStore


def test_batch_cache_is_split_per_id_and_reuses_hot_records() -> None:
    durable = MemoryStore()
    hot = MemoryStore()
    coordinator = IdCacheCoordinator(durable, hot, refresh_interval_seconds=10)
    calls: list[list[str]] = []

    def loader(keys: list[str]) -> list[dict[str, int]]:
        calls.append(keys)
        return [{"id": int(key), "name": f"Pilot {key}"} for key in keys]

    try:
        def splitter(payload: list[dict[str, int]]) -> dict[str, dict[str, int]]:
            return {str(item["id"]): item for item in payload}

        values, statuses = coordinator.fetch_batch("resolve_names", [1, 2, 1], loader, splitter)
        assert values == {"1": {"id": 1, "name": "Pilot 1"}, "2": {"id": 2, "name": "Pilot 2"}}
        assert statuses == {"1": "miss", "2": "miss"}
        values, statuses = coordinator.fetch_batch("resolve_names", [2, 1], loader, splitter)
        assert statuses == {"2": "hit", "1": "hit"}
        assert calls == [["1", "2"]]
    finally:
        coordinator.close()


def test_endpoint_ttls_follow_the_data_lifetime_policy() -> None:
    durable = MemoryStore()
    coordinator = IdCacheCoordinator(
        durable,
        ttl_seconds=60,
        ttl_by_endpoint={"resolve_names": 30 * 86400, "get_character": 2 * 86400},
        refresh_interval_seconds=10,
    )

    try:
        def loader(keys: list[str]) -> dict[str, str]:
            return {key: f"value-{key}" for key in keys}

        for endpoint in ("resolve_names", "get_character"):
            coordinator.fetch_batch(endpoint, ["42"], loader, lambda payload: dict(payload))

        names = durable.get_many([("resolve_names", "42")])[('resolve_names', '42')]
        character = durable.get_many([("get_character", "42")])[('get_character', '42')]
        assert 30 * 86400 - 1 <= names.expires_at - names.fetched_at <= 30 * 86400 + 1
        assert 2 * 86400 - 1 <= character.expires_at - character.fetched_at <= 2 * 86400 + 1
        assert coordinator.health()["ttl_by_endpoint"]["resolve_names"] == 30 * 86400
    finally:
        coordinator.close()


def test_stale_value_is_served_and_queued_for_background_refresh() -> None:
    durable = MemoryStore()
    coordinator = IdCacheCoordinator(
        durable,
        ttl_seconds=0.01,
        stale_grace_seconds=10,
        refresh_interval_seconds=10,
    )
    calls: list[list[str]] = []
    now = time.time()
    durable.put_many(
        [
            CacheRecord(
                endpoint="systems",
                entity_key="42",
                payload="value-old",
                fetched_at=now - 2,
                expires_at=now - 1,
                stale_until=now + 10,
            )
        ]
    )

    def loader(keys: list[str]) -> dict[str, str]:
        calls.append(keys)
        return {key: f"value-{len(calls)}" for key in keys}

    try:
        def splitter(payload: dict[str, str]) -> dict[str, str]:
            return dict(payload)

        stale, stale_status = coordinator.fetch_batch("systems", [42], loader, splitter)
        assert stale == {"42": "value-old"}
        assert stale_status == {"42": "stale"}
        coordinator._run_refresh_batch()
        refreshed, refreshed_status = coordinator.fetch_batch("systems", [42], loader, splitter)
        assert refreshed == {"42": "value-1"}
        assert refreshed_status == {"42": "hit"}
        assert calls == [["42"]]
    finally:
        coordinator.close()


def test_refresh_failure_keeps_old_value_and_records_retry() -> None:
    durable = MemoryStore()
    coordinator = IdCacheCoordinator(durable, ttl_seconds=0.01, stale_grace_seconds=10, refresh_interval_seconds=10)
    now = time.time()
    durable.put_many(
        [
            CacheRecord(
                endpoint="systems",
                entity_key="42",
                payload={"name": "Jita"},
                fetched_at=now - 2,
                expires_at=now - 1,
                stale_until=now + 10,
            )
        ]
    )

    try:
        def loader(_keys: list[str]) -> dict[str, object]:
            raise RuntimeError("ESI offline")

        values, statuses = coordinator.fetch_batch("systems", [42], loader, lambda payload: dict(payload))
        assert values == {"42": {"name": "Jita"}}
        assert statuses == {"42": "stale"}
        coordinator._run_refresh_batch()
        record = durable.get_many([("systems", "42")])[('systems', '42')]
        assert record.payload == {"name": "Jita"}
        assert record.failure_count == 1
        assert record.next_retry_at is not None
    finally:
        coordinator.close()
