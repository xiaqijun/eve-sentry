import json

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
