from app.core.models import Observation
from app.intel.enrichment import ThreatEnricher


class FakeResolver:
    def __init__(self):
        self.calls = []

    def character_profile(self, character_id):
        self.calls.append(character_id)
        if character_id == 123:
            return {"character_id": 123, "corporation_id": 42}
        raise RuntimeError("offline")


class FakeKillboard:
    def __init__(self):
        self.calls = []

    def character_recent(self, character_id):
        self.calls.append(character_id)
        if character_id == 123:
            return [
                {
                    "killmail_id": 1,
                    "killmail_time": "2026-06-30T12:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 999, "ship_type_id": 111},
                    "attackers": [{"character_id": 123}],
                }
            ]
        raise RuntimeError("offline")


def test_threat_enricher_collects_profiles_and_kill_activity():
    resolver = FakeResolver()
    killboard = FakeKillboard()
    enricher = ThreatEnricher(
        resolver=resolver,
        killboard=killboard,
        kill_window="7d",
    )
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        names=["Alice", "Bob"],
        character_ids=[123, 123, 456],
    )

    enrichment = enricher.enrich(observation)

    assert enrichment.has_data()
    assert enrichment.character_profiles == [{"character_id": 123, "corporation_id": 42}]
    assert len(enrichment.kill_activities) == 1
    assert enrichment.kill_activities[0].character_id == 123
    assert enrichment.kill_activities[0].kills == 1
    assert enrichment.kill_activities[0].window == "7d"
    assert resolver.calls == [123, 456]
    assert killboard.calls == [123, 456]


def test_threat_enricher_returns_empty_data_without_sources():
    enricher = ThreatEnricher()
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        names=["Alice"],
        character_ids=[123],
    )

    assert not enricher.enrich(observation).has_data()
