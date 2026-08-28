import threading
import time

from app.core.models import Observation
from app.esi.cache import EsiCache
from app.esi.client import EsiApiError
from app.esi.resolver import EsiResolver


class FakeEsiClient:
    def __init__(self):
        self.resolve_calls = 0
        self.character_calls = 0
        self.corporation_calls = 0
        self.alliance_calls = 0
        self.system_calls = 0

    def resolve_ids(self, names):
        self.resolve_calls += 1
        if names == ["Alice", "Tama"]:
            return {
                "characters": [{"id": 123, "name": "Alice"}],
                "systems": [{"id": 30002813, "name": "Tama"}],
            }
        if names == ["Alice"]:
            return {
                "characters": [{"id": 123, "name": "Alice"}],
            }
        raise AssertionError(names)

    def get_character(self, character_id):
        self.character_calls += 1
        return {
            "name": "Alice",
            "corporation_id": 456,
            "alliance_id": 789,
            "security_status": -4.2,
        }

    def get_corporation(self, corporation_id):
        self.corporation_calls += 1
        return {
            "name": "Some Corp",
            "ticker": "SC",
            "alliance_id": 789,
        }

    def get_alliance(self, alliance_id):
        self.alliance_calls += 1
        return {
            "name": "Some Alliance",
            "ticker": "SA",
        }

    def get_system(self, system_id):
        self.system_calls += 1
        return {
            "name": "Tama",
            "constellation_id": 20000442,
            "security_status": 0.3,
        }


class FailingEsiClient(FakeEsiClient):
    def resolve_ids(self, names):
        raise EsiApiError("offline")


class FailingAffiliationClient(FakeEsiClient):
    def get_corporation(self, corporation_id):
        raise EsiApiError("corporation offline")

    def get_alliance(self, alliance_id):
        raise EsiApiError("alliance offline")


class PartialResolutionClient(FakeEsiClient):
    def resolve_ids(self, names):
        self.resolve_calls += 1
        assert names == ["Ghost Pilot", "Tama"]
        return {
            "systems": [{"id": 30002813, "name": "Tama"}],
        }


def test_resolve_names_uses_client_then_cache(tmp_path):
    client = FakeEsiClient()
    resolver = EsiResolver(client=client, cache=EsiCache(tmp_path / "esi.json"))

    first = resolver.resolve_names(["Alice", "Tama"])
    second = resolver.resolve_names(["Alice", "Tama"])

    assert [(item.category, item.name, item.entity_id) for item in first] == [
        ("character", "Alice", 123),
        ("solar_system", "Tama", 30002813),
    ]
    assert [(item.category, item.name, item.entity_id) for item in second] == [
        ("character", "Alice", 123),
        ("solar_system", "Tama", 30002813),
    ]
    assert client.resolve_calls == 1


def test_cached_identity_access_never_calls_esi(tmp_path):
    class NetworkMustNotRun:
        def resolve_ids(self, names):
            raise AssertionError(names)

        def get_character(self, character_id):
            raise AssertionError(character_id)

    cache = EsiCache(tmp_path / "esi.json")
    cache.set(
        "name:alice",
        {"name": "Alice", "category": "character", "id": 123},
    )
    cache.set(
        "character:123",
        {
            "character_id": 123,
            "name": "Alice",
            "corporation_id": 456,
            "corporation_name": "Some Corp",
        },
    )
    resolver = EsiResolver(client=NetworkMustNotRun(), cache=cache)

    resolved, name_status = resolver.cached_name("Alice")
    profile = resolver.cached_character_profile(123)

    assert resolved is not None
    assert (resolved.name, resolved.category, resolved.entity_id) == (
        "Alice",
        "character",
        123,
    )
    assert name_status == "cached"
    assert profile is not None
    assert profile["corporation_name"] == "Some Corp"
    assert profile["cache_status"] == "cached"


def test_cached_identity_can_return_stale_data_for_background_refresh(tmp_path):
    cache = EsiCache(tmp_path / "esi.json")
    cache._items = {
        "name:alice": {
            "value": {"name": "Alice", "category": "character", "id": 123},
            "expires_at": 0,
        },
        "character:123": {
            "value": {"character_id": 123, "name": "Alice"},
            "expires_at": 0,
        },
    }
    resolver = EsiResolver(client=FailingEsiClient(), cache=cache)

    resolved, name_status = resolver.cached_name("Alice", allow_stale=True)
    profile = resolver.cached_character_profile(123, allow_stale=True)

    assert resolved is not None
    assert name_status == "stale"
    assert profile is not None
    assert profile["cache_status"] == "stale"


def test_resolve_names_negative_caches_unresolved_names(tmp_path):
    class MissingNameClient(FakeEsiClient):
        def resolve_ids(self, names):
            self.resolve_calls += 1
            assert names == ["Ghost Pilot"]
            return {}

    client = MissingNameClient()
    resolver = EsiResolver(client=client, cache=EsiCache(tmp_path / "esi.json"))

    assert resolver.resolve_names(["Ghost Pilot"]) == []
    assert resolver.resolve_names(["Ghost Pilot"]) == []

    assert client.resolve_calls == 1


def test_negative_name_cache_uses_short_ttl(tmp_path):
    class MissingNameClient(FakeEsiClient):
        def resolve_ids(self, names):
            self.resolve_calls += 1
            return {}

    cache = EsiCache(tmp_path / "esi.json")
    resolver = EsiResolver(
        client=MissingNameClient(),
        cache=cache,
        negative_ttl_seconds=120,
    )

    assert resolver.resolve_names(["Ghost Pilot"]) == []
    metadata = cache.metadata("name:ghost pilot")

    assert 0 < metadata["expires_at"] - metadata["fetched_at"] <= 120


def test_concurrent_name_batches_share_one_esi_request(tmp_path):
    class BlockingClient(FakeEsiClient):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def resolve_ids(self, names):
            self.resolve_calls += 1
            self.started.set()
            assert self.release.wait(timeout=1)
            return {"characters": [{"id": 123, "name": "Alice"}]}

    client = BlockingClient()
    resolver = EsiResolver(client=client, cache=EsiCache(tmp_path / "esi.json"))
    results = []

    first = threading.Thread(target=lambda: results.append(resolver.resolve_names(["Alice"])))
    second = threading.Thread(target=lambda: results.append(resolver.resolve_names(["Alice"])))
    first.start()
    assert client.started.wait(timeout=1)
    second.start()
    time.sleep(0.02)
    client.release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive() and not second.is_alive()
    assert client.resolve_calls == 1
    assert len(results) == 2


def test_character_profile_includes_stable_zkill_link(tmp_path):
    resolver = EsiResolver(
        client=FakeEsiClient(),
        cache=EsiCache(tmp_path / "esi.json"),
    )

    profile = resolver.character_profile(123)

    assert profile["zkill_url"] == "https://zkillboard.com/character/123/"


def test_profiles_are_cached(tmp_path):
    client = FakeEsiClient()
    resolver = EsiResolver(client=client, cache=EsiCache(tmp_path / "esi.json"))

    character = resolver.character_profile(123)
    cached_character = resolver.character_profile(123)
    assert resolver.system_profile(30002813)["name"] == "Tama"
    assert resolver.system_profile(30002813)["name"] == "Tama"

    assert character["corporation_id"] == 456
    assert character["corporation_name"] == "Some Corp"
    assert character["alliance_id"] == 789
    assert character["alliance_name"] == "Some Alliance"
    assert character["cache_status"] == "refreshed"
    assert cached_character["corporation_name"] == "Some Corp"
    assert cached_character["cache_status"] == "cached"
    assert cached_character["fetched_at"] > 0
    assert cached_character["expires_at"] > cached_character["fetched_at"]
    assert client.character_calls == 1
    assert client.corporation_calls == 1
    assert client.alliance_calls == 1
    assert client.system_calls == 1


def test_affiliation_lookup_failure_keeps_character_profile(tmp_path):
    resolver = EsiResolver(
        client=FailingAffiliationClient(),
        cache=EsiCache(tmp_path / "esi.json"),
    )

    profile = resolver.character_profile(123)

    assert profile["character_id"] == 123
    assert profile["corporation_id"] == 456
    assert profile["alliance_id"] == 789
    assert "corporation_name" not in profile
    assert "alliance_name" not in profile


def test_corporation_and_alliance_profiles_are_cached(tmp_path):
    client = FakeEsiClient()
    resolver = EsiResolver(client=client, cache=EsiCache(tmp_path / "esi.json"))

    assert resolver.corporation_profile(456)["name"] == "Some Corp"
    assert resolver.corporation_profile(456)["name"] == "Some Corp"
    assert resolver.alliance_profile(789)["name"] == "Some Alliance"
    assert resolver.alliance_profile(789)["name"] == "Some Alliance"

    assert client.corporation_calls == 1
    assert client.alliance_calls == 1


def test_enrich_observation_fills_ids(tmp_path):
    resolver = EsiResolver(
        client=FakeEsiClient(),
        cache=EsiCache(tmp_path / "esi.json"),
    )
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        names=["Alice"],
    )

    enriched = resolver.enrich_observation(observation)

    assert enriched.system_id == 30002813
    assert enriched.character_ids == [123]


def test_enrich_observation_records_resolution_metadata(tmp_path):
    resolver = EsiResolver(
        client=PartialResolutionClient(),
        cache=EsiCache(tmp_path / "esi.json"),
    )
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        names=["Ghost Pilot"],
    )

    enriched = resolver.enrich_observation(observation)
    resolution = enriched.metadata["esi_resolution"]

    assert enriched.system_id == 30002813
    assert enriched.character_ids == []
    assert resolution == {
        "attempted": True,
        "character_name_count": 1,
        "resolved_character_count": 0,
        "system_name_matched": True,
        "unresolved_character_names": ["Ghost Pilot"],
        "resolved_system_id": 30002813,
    }


def test_enrich_observation_preserves_existing_resolution_metadata(tmp_path):
    resolver = EsiResolver(
        client=FakeEsiClient(),
        cache=EsiCache(tmp_path / "esi.json"),
    )
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        system_id=30002813,
        names=["Alice"],
        metadata={
            "esi_resolution": {
                "candidate_system_names": ["Alice", "Tama"],
                "resolved_system_candidates": ["Tama"],
                "system_repair_status": "repaired",
                "system_repaired_from": "Alice",
                "system_repaired_to": "Tama",
            }
        },
    )

    enriched = resolver.enrich_observation(observation)
    resolution = enriched.metadata["esi_resolution"]

    assert enriched.character_ids == [123]
    assert resolution == {
        "attempted": True,
        "candidate_system_names": ["Alice", "Tama"],
        "resolved_character_count": 1,
        "resolved_character_names": ["Alice"],
        "resolved_system_candidates": ["Tama"],
        "resolved_system_id": 30002813,
        "system_name_matched": True,
        "system_repair_status": "repaired",
        "system_repaired_from": "Alice",
        "system_repaired_to": "Tama",
        "character_name_count": 1,
    }


def test_enrich_observation_returns_original_on_esi_failure(tmp_path):
    resolver = EsiResolver(
        client=FailingEsiClient(),
        cache=EsiCache(tmp_path / "esi.json"),
    )
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        names=["Alice"],
    )

    enriched = resolver.enrich_observation(observation)

    assert enriched is observation
    assert enriched.system_id is None
    assert enriched.character_ids == []
