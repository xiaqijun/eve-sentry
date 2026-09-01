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


def test_hostile_icon_alerts_when_standing_is_unavailable():
    event = ClassificationEngine(cooldown_seconds=0).score(
        Observation(
            source="eve-sentry-detector",
            system_name="S-KSWL",
            names=["Sundeezl Hopkins"],
            character_ids=[92358740],
            raw_text="Sundeezl Hopkins",
            metadata={"hostile_icon_count": 1},
            seen_at="2026-07-09T10:00:00+00:00",
            received_at="2026-07-09T10:00:01+00:00",
            observation_id="obs-hostile-icon",
        )
    )

    assert event is not None
    assert event.classification == "red"
    assert evidence_types(event)[0] == "hostile_icon"


def test_friendly_identity_is_not_overridden_by_shared_detector_icon_count():
    engine = ClassificationEngine(cooldown_seconds=0)

    event = engine.score(
        Observation(
            source="eve-sentry-detector",
            system_name="S-KSWL",
            names=["chin harry"],
            character_ids=[92358740],
            raw_text="chin harry",
            metadata={"hostile_icon_count": 16},
            seen_at="2026-07-09T10:00:00+00:00",
            received_at="2026-07-09T10:00:01+00:00",
            observation_id="obs-friendly-icon",
        ),
        character_profile={
            "character_id": 92358740,
            "contact_standing": 10.0,
        },
    )

    assert event is not None
    assert event.classification == "white"
    assert evidence_types(event)[:1] == ["friendly_standing"]


def test_friendly_alliance_overrides_neutral_standing_and_shared_icon_count():
    engine = ClassificationEngine(
        watchlist=Watchlist(friendly_alliance_ids={99003581}),
        cooldown_seconds=0,
    )

    event = engine.score(
        Observation(
            source="eve-sentry-detector",
            system_name="HB-FSO",
            names=["Samcat"],
            character_ids=[2115746410],
            raw_text="Samcat",
            metadata={"hostile_icon_count": 2},
            seen_at="2026-09-01T13:48:02+00:00",
            received_at="2026-09-01T13:48:03+00:00",
            observation_id="obs-samcat-friendly-alliance",
        ),
        character_profile={
            "character_id": 2115746410,
            "corporation_id": 98524084,
            "alliance_id": 99003581,
            "contact_standing": 0.0,
        },
    )

    assert event is not None
    assert event.classification == "white"
    assert evidence_types(event)[:1] == ["friendly_alliance"]


def test_friendly_alliance_does_not_override_non_neutral_threshold_match():
    engine = ClassificationEngine(
        watchlist=Watchlist(
            friendly_alliance_ids={99003581},
            hostile_standing_threshold=3.0,
        ),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={
            "character_id": 2115746410,
            "alliance_id": 99003581,
            "contact_standing": 2.0,
        },
    )

    assert event is not None
    assert event.classification == "red"
    assert evidence_types(event)[:1] == ["hostile_standing"]


def test_friendly_profile_does_not_hide_another_neutral_profile():
    engine = ClassificationEngine(
        watchlist=Watchlist(friendly_alliance_ids={99003581}),
        cooldown_seconds=0,
    )

    event = engine.score(
        Observation(
            source="intel_channel",
            system_name="HB-FSO",
            names=["Samcat", "Neutral Pilot"],
            character_ids=[2115746410, 123],
            raw_text="Samcat Neutral Pilot",
            seen_at="2026-09-01T13:48:02+00:00",
            received_at="2026-09-01T13:48:03+00:00",
            observation_id="obs-mixed-friendly-neutral",
        ),
        character_profiles=[
            {
                "character_id": 2115746410,
                "alliance_id": 99003581,
                "contact_standing": 0.0,
            },
            {"character_id": 123, "contact_standing": 0.0},
        ],
    )

    assert event is not None
    assert event.classification == "red"
    assert evidence_types(event)[:1] == ["hostile_standing"]


def test_friendly_alliance_does_not_override_negative_standing():
    engine = ClassificationEngine(
        watchlist=Watchlist(friendly_alliance_ids={99003581}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={
            "character_id": 2115746410,
            "alliance_id": 99003581,
            "contact_standing": -5.0,
        },
    )

    assert event is not None
    assert event.classification == "red"
    assert evidence_types(event)[:1] == ["hostile_standing"]


def test_name_whitelist_does_not_override_neutral_standing():
    engine = ClassificationEngine(
        watchlist=Watchlist(whitelist={"Samcat"}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel", names=["Samcat"]),
        character_profile={"character_id": 2115746410, "contact_standing": 0.0},
    )

    assert event is not None
    assert event.classification == "red"
    assert evidence_types(event)[:1] == ["hostile_standing"]


def test_disabled_hostile_standing_rule_ignores_negative_standing():
    engine = ClassificationEngine(
        watchlist=Watchlist(hostile_standing_threshold=None),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "contact_standing": -5.0},
    )

    assert event is None


def test_hostile_standing_uses_configured_threshold_for_negative_values():
    engine = ClassificationEngine(
        watchlist=Watchlist(hostile_standing_threshold=-10.0),
        cooldown_seconds=0,
    )

    below_threshold = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "contact_standing": -5.0},
    )
    at_threshold = engine.score(
        Observation(
            source="intel_channel",
            system_name="S-KSWL",
            names=["Alice"],
            raw_text="Alice",
            seen_at="2026-07-09T10:00:00+00:00",
            received_at="2026-07-09T10:00:01+00:00",
            observation_id="obs-threshold-negative",
        ),
        character_profile={"character_id": 123, "contact_standing": -10.0},
    )

    assert below_threshold is None
    assert at_threshold is not None
    assert at_threshold.classification == "red"
    assert evidence_types(at_threshold)[:1] == ["hostile_standing"]
