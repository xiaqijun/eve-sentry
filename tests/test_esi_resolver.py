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

    character = resolver.character_profile(123)
    cached_character = resolver.character_profile(123)
    assert resolver.system_profile(30002813)["name"] == "Tama"
    assert resolver.system_profile(30002813)["name"] == "Tama"

    assert character["corporation_id"] == 456
    assert character["corporation_name"] == "Some Corp"
    assert character["alliance_id"] == 789
    assert character["alliance_name"] == "Some Alliance"
    assert cached_character["corporation_name"] == "Some Corp"
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
