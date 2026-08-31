import json
import time

from app.esi.cache import EsiCache


def test_esi_cache_persists_values(tmp_path):
    path = tmp_path / "esi_cache.json"
    cache = EsiCache(path)

    cache.set("name:alice", {"id": 1}, ttl_seconds=60)
    cache.save()

    assert EsiCache(path).get("name:alice") == {"id": 1}
    metadata = EsiCache(path).metadata("name:alice")
    assert metadata["cache_status"] == "cached"
    assert metadata["fetched_at"] > 0
    assert metadata["expires_at"] > metadata["fetched_at"]


def test_esi_cache_ignores_expired_values(tmp_path):
    path = tmp_path / "esi_cache.json"
    path.write_text(
        json.dumps({"name:alice": {"value": {"id": 1}, "expires_at": 0}}),
        encoding="utf-8",
    )

    cache = EsiCache(path)

    assert cache.get("name:alice") is None
    assert cache.get_stale("name:alice") == {"id": 1}
    assert cache.metadata("name:alice")["cache_status"] == "stale"
    assert cache.metadata("name:bob") == {"cache_status": "miss"}

    snapshot = cache.snapshot()
    assert snapshot["totals"] == {
        "lookups": 1,
        "hits": 0,
        "misses": 1,
        "stale_hits": 1,
        "evictions": 0,
        "hit_rate": 0.0,
        "lookup_rate_per_second": 0.0167,
    }
    assert snapshot["entries"] == {"total": 1, "active": 0, "stale": 1}
    assert snapshot["namespaces"]["name"]["misses"] == 1
    assert snapshot["namespaces"]["name"]["stale_hits"] == 1


def test_esi_cache_snapshot_counts_personnel_lookups_per_name(tmp_path):
    cache = EsiCache(tmp_path / "esi_cache.json")
    cache.set("name:alice", {"id": 1}, ttl_seconds=60)
    cache.set("name:bob", {"id": 2}, ttl_seconds=60)

    assert cache.get("name:alice") == {"id": 1}
    assert cache.get("name:bob") == {"id": 2}
    assert cache.get("name:carol") is None

    names = cache.snapshot()["namespaces"]["name"]
    assert names["lookups"] == 3
    assert names["hits"] == 2
    assert names["misses"] == 1
    assert names["hit_rate"] == 0.6667
    assert names["active_entries"] == 2


def test_esi_cache_prunes_old_stale_entries_and_keeps_recent_stale(tmp_path):
    path = tmp_path / "esi_cache.json"
    now = time.time()
    path.write_text(
        json.dumps(
            {
                "name:old": {
                    "value": {"id": 1},
                    "fetched_at": now - 10 * 86400,
                    "expires_at": now - 9 * 86400,
                },
                "name:recent": {
                    "value": {"id": 2},
                    "fetched_at": now - 3600,
                    "expires_at": now - 60,
                },
            }
        ),
        encoding="utf-8",
    )

    cache = EsiCache(path, stale_grace_seconds=7 * 86400)

    assert cache.get_stale("name:old") is None
    assert cache.get_stale("name:recent") == {"id": 2}
    assert cache.snapshot()["entries"] == {"total": 1, "active": 0, "stale": 1}


def test_esi_cache_save_is_atomic_and_supports_namespace_invalidation(tmp_path):
    path = tmp_path / "esi_cache.json"
    cache = EsiCache(path)
    cache.set("character:1", {"name": "Alice"}, ttl_seconds=60)
    cache.set("corporation:2", {"name": "Corp"}, ttl_seconds=60)

    assert cache.invalidate_namespace("character") == 1
    cache.save()

    assert EsiCache(path).get("character:1") is None
    assert EsiCache(path).get("corporation:2") == {"name": "Corp"}
    assert list(path.parent.glob("*.tmp")) == []
