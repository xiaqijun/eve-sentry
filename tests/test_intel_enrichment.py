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


def test_threat_enricher_collects_profiles_without_killboard_activity():
    resolver = FakeResolver()
    enricher = ThreatEnricher(
        resolver=resolver,
        killboard=object(),
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
    assert enrichment.kill_activities == []
    assert enrichment.group_activities == []
    assert resolver.calls == [123, 456]


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


def test_threat_enricher_marks_unmatched_authenticated_contact_as_neutral():
    class FakeSession:
        def snapshot(self, include_location=True, include_contacts=True):
            return SimpleNamespace(
                contacts=[
                    ContactStanding(
                        contact_id=999,
                        contact_type="character",
                        standing=10,
                    )
                ]
            )

    enricher = ThreatEnricher(
        resolver=FakeResolver(),
        esi_session=FakeSession(),
    )
    observation = Observation(
        source="local_ocr",
        system_name="Tama",
        names=["Alice"],
        character_ids=[123],
    )

    enrichment = enricher.enrich(observation)
    profile = enrichment.character_profiles[0]

    assert profile["character_id"] == 123
    assert profile["corporation_id"] == 456
    assert profile["alliance_id"] == 789
    assert profile["contact_standing"] == 0.0
    assert profile["standing_source"] == "esi_contacts"
    assert profile["standing_contact_type"] == "neutral"


def test_threat_enricher_keeps_last_contact_snapshot_when_esi_fails():
    class FlakySession:
        def __init__(self):
            self.calls = 0

        def snapshot(self, include_location=True, include_contacts=True):
            self.calls += 1
            if self.calls == 1:
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
            raise RuntimeError("ESI token temporarily unavailable")

    session = FlakySession()
    clock = iter((1000.0, 1061.0))
    enricher = ThreatEnricher(
        resolver=FakeResolver(),
        esi_session=session,
        standing_ttl_seconds=60,
        now=lambda: next(clock),
    )
    observation = Observation(
        source="local_ocr",
        system_name="Tama",
        names=["Alice"],
        character_ids=[123],
    )

    first = enricher.enrich(observation).character_profiles[0]
    second = enricher.enrich(observation).character_profiles[0]

    assert first["contact_standing"] == -10.0
    assert second["contact_standing"] == -10.0
    assert second["standing_contact_id"] == 456
    assert session.calls == 2


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


def test_threat_enricher_exposes_system_profile_without_activity_lookup():
    enricher = ThreatEnricher(
        resolver=FakeResolver(),
        killboard=object(),
    )

    profile = enricher.system_profile(30002813)

    assert profile == {"system_id": 30002813, "name": "Tama"}
    assert not hasattr(enricher, "system_kill_activity")


def test_threat_enricher_delegates_character_name_completion():
    class FakeSession:
        def complete_character_name(self, prefix):
            assert prefix == "Kamamdzhava Teker"
            return "Kamamdzhava Tekerav Longname"

    enricher = ThreatEnricher(esi_session=FakeSession())

    assert (
        enricher.complete_character_name("Kamamdzhava Teker")
        == "Kamamdzhava Tekerav Longname"
    )
