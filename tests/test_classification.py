from app.core.models import Observation
from app.intel.classification import CLASSIFICATION_VERSION, ClassificationEngine
from app.intel.scoring import Watchlist


def observation(names=None, source="local_ocr"):
    return Observation(
        source=source,
        system_name="S-KSWL",
        names=["Alice"] if names is None else list(names),
        raw_text="Alice",
        seen_at="2026-07-09T10:00:00+00:00",
        received_at="2026-07-09T10:00:01+00:00",
        observation_id=f"obs-{source}",
    )


def evidence_types(event):
    return [item.evidence_type for item in event.evidence]


def test_unknown_observation_does_not_alert():
    event = ClassificationEngine(cooldown_seconds=0).score(observation())

    assert event is None


def test_blacklisted_name_alerts_as_hostile():
    engine = ClassificationEngine(
        watchlist=Watchlist(blacklist={"Alice"}),
        cooldown_seconds=0,
    )

    event = engine.score(observation())

    assert event is not None
    assert event.classification == "red"
    assert event.reason == "Hostile pilot name Alice"
    assert event.score == 100
    assert event.level == "critical"
    assert event.scoring_version == CLASSIFICATION_VERSION
    assert evidence_types(event)[:1] == ["hostile_name"]


def test_whitelisted_name_alerts_as_friendly():
    engine = ClassificationEngine(
        watchlist=Watchlist(whitelist={"Alice"}),
        cooldown_seconds=0,
    )

    event = engine.score(observation())

    assert event is not None
    assert event.classification == "white"
    assert event.reason == "Friendly pilot name Alice"
    assert event.score == 1
    assert event.level == "low"
    assert evidence_types(event)[:1] == ["friendly_name"]


def test_hostile_profile_wins_over_friendly_name():
    engine = ClassificationEngine(
        watchlist=Watchlist(
            whitelist={"Alice"},
            hostile_corporation_ids={42},
        ),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(),
        character_profile={"character_id": 123, "corporation_id": 42},
    )

    assert event is not None
    assert event.classification == "red"
    assert evidence_types(event)[:1] == ["hostile_corporation"]


def test_friendly_profile_alerts_without_hiding_observation():
    engine = ClassificationEngine(
        watchlist=Watchlist(friendly_alliance_ids={77}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "alliance_id": 77},
    )

    assert event is not None
    assert event.classification == "white"
    assert engine.suppresses_observation(observation()) is False


def test_neutral_standing_alerts_as_hostile_by_default():
    event = ClassificationEngine(cooldown_seconds=0).score(
        observation(),
        character_profile={"character_id": 123, "contact_standing": 0.0},
    )

    assert event is not None
    assert event.classification == "red"
    assert event.reason == "Hostile standing 0"
    assert evidence_types(event)[:1] == ["hostile_standing"]
