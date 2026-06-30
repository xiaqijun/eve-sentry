from app.core.models import Observation
from app.esi.cache import EsiCache
from app.esi.client import EsiApiError
from app.esi.resolver import EsiResolver


class FakeEsiClient:
    def __init__(self):
        self.resolve_calls = 0
        self.character_calls = 0
        self.system_calls = 0

    def resolve_ids(self, names):
        self.resolve_calls += 1
        assert names == ["Alice", "Tama"]
        return {
            "characters": [{"id": 123, "name": "Alice"}],
            "systems": [{"id": 30002813, "name": "Tama"}],
        }

    def get_character(self, character_id):
        self.character_calls += 1
        return {
            "name": "Alice",
            "corporation_id": 456,
            "alliance_id": 789,
            "security_status": -4.2,
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


def test_profiles_are_cached(tmp_path):
    client = FakeEsiClient()
    resolver = EsiResolver(client=client, cache=EsiCache(tmp_path / "esi.json"))

    assert resolver.character_profile(123)["corporation_id"] == 456
    assert resolver.character_profile(123)["corporation_id"] == 456
    assert resolver.system_profile(30002813)["name"] == "Tama"
    assert resolver.system_profile(30002813)["name"] == "Tama"

    assert client.character_calls == 1
    assert client.system_calls == 1


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

