from types import SimpleNamespace

from app.core.models import Observation
from app.esi.session import ContactStanding
from app.intel.enrichment import ThreatEnricher


class FakeResolver:
    def __init__(self):
        self.calls = []

    def character_profile(self, character_id):
        self.calls.append(character_id)
        if character_id == 123:
            return {
                "character_id": 123,
                "corporation_id": 456,
                "alliance_id": 789,
            }
        raise RuntimeError("offline")

    def system_profile(self, system_id):
        if system_id == 30002813:
            return {"system_id": 30002813, "name": "Tama"}
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

    def system_recent(self, system_id):
        if system_id == 30002813:
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

    def corporation_recent(self, corporation_id):
        if corporation_id == 456:
            return [
                {
                    "killmail_id": 2,
                    "killmail_time": "2026-06-30T13:00:00Z",
                    "solar_system_id": 30002814,
                    "victim": {"character_id": 999, "corporation_id": 777},
                    "attackers": [{"character_id": 123, "corporation_id": 456}],
                }
            ]
        raise RuntimeError("offline")

    def alliance_recent(self, alliance_id):
        if alliance_id == 789:
            return [
                {
                    "killmail_id": 3,
                    "killmail_time": "2026-06-30T14:00:00Z",
                    "solar_system_id": 30002815,
                    "victim": {"character_id": 456, "alliance_id": 789},
                    "attackers": [{"character_id": 123, "alliance_id": 111}],
                }
            ]
        raise RuntimeError("offline")

    def activity_status(self, scope, entity_id):
        return {
            "cache_status": "cached",
            "fetched_at": 10.0,
            "expires_at": 70.0,
            "request_status": "backoff",
            "error": "zKillboard HTTP 429",
            "retry_after": 130.0,
        }


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
    assert enrichment.character_profiles == [
        {"character_id": 123, "corporation_id": 456, "alliance_id": 789}
    ]
    assert len(enrichment.kill_activities) == 1
    assert enrichment.kill_activities[0].character_id == 123
    assert enrichment.kill_activities[0].kills == 1
    assert enrichment.kill_activities[0].window == "7d"
    assert enrichment.kill_activities[0].cache_status == "cached"
    assert enrichment.kill_activities[0].fetched_at == 10.0
    assert enrichment.kill_activities[0].request_status == "backoff"
    assert enrichment.kill_activities[0].error == "zKillboard HTTP 429"
    assert enrichment.kill_activities[0].retry_after == 130.0
    assert [item.entity_type for item in enrichment.group_activities] == [
        "corporation",
        "alliance",
    ]
    assert enrichment.group_activities[0].cache_status == "cached"
    assert resolver.calls == [123, 456]
    assert killboard.calls == [123, 456]


def test_threat_enricher_applies_authenticated_contact_standings():
    class FakeSession:
        def __init__(self):
            self.calls = []

        def snapshot(self, include_location=True, include_contacts=True):
            self.calls.append((include_location, include_contacts))
            return SimpleNamespace(
                contacts=[
                    ContactStanding(
                        contact_id=456,
                        contact_type="corporation",
                        standing=-10,
                        label="Hostile Corp",
                    )
                ]
            )

    session = FakeSession()
    enricher = ThreatEnricher(
        resolver=FakeResolver(),
        esi_session=session,
        standing_ttl_seconds=60,
        now=lambda: 1000,
    )
    observation = Observation(
        source="local_ocr",
        system_name="Tama",
        names=["Alice"],
        character_ids=[123],
    )

    enrichment = enricher.enrich(observation)
    profile = enrichment.character_profiles[0]

    assert profile["contact_standing"] == -10.0
    assert profile["standing_contact_id"] == 456
    assert profile["standing_contact_type"] == "corporation"
    assert profile["standing_label"] == "Hostile Corp"
    assert session.calls == [(False, True)]


def test_threat_enricher_scores_direct_contact_without_public_profile():
    class EmptyResolver:
        def character_profile(self, character_id):
            raise RuntimeError("offline")

    class FakeSession:
        def snapshot(self, include_location=True, include_contacts=True):
            return SimpleNamespace(
                contacts=[
                    ContactStanding(
                        contact_id=123,
                        contact_type="character",
                        standing=-5,
                    )
                ]
            )

    enricher = ThreatEnricher(resolver=EmptyResolver(), esi_session=FakeSession())
    observation = Observation(
        source="local_ocr",
        system_name="Tama",
        names=["Alice"],
        character_ids=[123],
    )

    enrichment = enricher.enrich(observation)

    assert enrichment.character_profiles == [
        {
            "character_id": 123,
            "contact_standing": -5.0,
            "standing_source": "esi_contacts",
            "standing_contact_id": 123,
            "standing_contact_type": "character",
        }
    ]


def test_threat_enricher_returns_empty_data_without_sources():
    enricher = ThreatEnricher()
    observation = Observation(
        source="intel_channel",
        system_name="Tama",
        names=["Alice"],
        character_ids=[123],
    )

    assert not enricher.enrich(observation).has_data()


def test_threat_enricher_exposes_system_profile_and_activity():
    enricher = ThreatEnricher(
        resolver=FakeResolver(),
        killboard=FakeKillboard(),
        kill_window="7d",
    )

    profile = enricher.system_profile(30002813)
    activity = enricher.system_kill_activity(30002813)

    assert profile == {"system_id": 30002813, "name": "Tama"}
    assert activity is not None
    assert activity.system_id == 30002813
    assert activity.kills == 1
    assert activity.window == "7d"


def test_threat_enricher_exposes_corporation_and_alliance_activity():
    enricher = ThreatEnricher(killboard=FakeKillboard(), kill_window="7d")

    corporation = enricher.corporation_kill_activity(456)
    alliance = enricher.alliance_kill_activity(789)

    assert corporation is not None
    assert corporation.entity_type == "corporation"
    assert corporation.entity_id == 456
    assert corporation.kills == 1
    assert corporation.losses == 0
    assert corporation.window == "7d"
    assert alliance is not None
    assert alliance.entity_type == "alliance"
    assert alliance.entity_id == 789
    assert alliance.kills == 0
    assert alliance.losses == 1
