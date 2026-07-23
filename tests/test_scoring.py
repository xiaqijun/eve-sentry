from types import SimpleNamespace

from app.core.models import Observation
from app.intel.scoring import SCORING_VERSION, ChannelMention, ScoringEngine, Watchlist


def observation(
    source="local_ocr",
    names=None,
    character_ids=None,
    system_name="Tama",
    raw_text="Tama Alice",
    confidence=None,
):
    return Observation(
        source=source,
        system_name=system_name,
        names=["Alice"] if names is None else list(names),
        character_ids=[123] if character_ids is None else list(character_ids),
        raw_text=raw_text,
        confidence=confidence,
        seen_at="2026-06-29T12:00:00+00:00",
        received_at="2026-06-29T12:00:01+00:00",
        observation_id=f"obs-{source}",
    )


def evidence_types(event):
    return [item.evidence_type for item in event.evidence]


def test_local_ocr_without_hostile_evidence_does_not_alert():
    event = ScoringEngine(cooldown_seconds=0).score(observation())

    assert event is None


def test_medium_confidence_local_ocr_with_hostile_evidence_is_downweighted():
    engine = ScoringEngine(
        watchlist=Watchlist(blacklist={"alice"}),
        cooldown_seconds=0,
    )

    event = engine.score(observation(confidence=0.5))

    assert event is not None
    assert event.score == 110
    assert event.level == "critical"
    assert event.evidence[0].weight == 30


def test_very_low_confidence_local_ocr_without_identity_support_is_suppressed():
    event = ScoringEngine(cooldown_seconds=0).score(
        observation(character_ids=[], confidence=0.2)
    )

    assert event is None


def test_low_confidence_intel_channel_without_target_is_suppressed():
    event = ScoringEngine(cooldown_seconds=0).score(
        observation(
            source="intel_channel",
            names=[],
            character_ids=[],
            system_name="Unknown",
            raw_text="Scout A: no useful structure here",
            confidence=0.2,
        )
    )

    assert event is None


def test_esi_resolution_suppresses_unresolved_named_target():
    item = observation(source="intel_channel", character_ids=[])
    item.metadata = {
        "esi_resolution": {
            "attempted": True,
            "character_name_count": 1,
            "resolved_character_count": 0,
            "system_name_matched": True,
            "unresolved_character_names": ["Alice"],
        }
    }

    event = ScoringEngine(cooldown_seconds=0).score(item)

    assert event is None


def test_esi_resolution_suppresses_ambiguous_repair_status_without_attempt_flag():
    item = observation(
        source="intel_channel",
        names=["Tama Oijanen"],
        character_ids=[],
        system_name="Alice",
        raw_text="Scout A: Alice reds Tama Oijanen",
    )
    item.metadata = {
        "hostile_count": 1,
        "esi_resolution": {
            "candidate_system_names": ["Alice", "Tama", "Oijanen"],
            "resolved_system_candidates": ["Tama", "Oijanen"],
            "system_repair_status": "ambiguous",
        },
    }

    event = ScoringEngine(cooldown_seconds=0).score(item)

    assert event is None


def test_esi_resolution_suppresses_unresolved_system_match():
    item = observation(
        source="intel_channel",
        names=[],
        character_ids=[],
        system_name="Alice",
        raw_text="Scout A: Alice reds",
    )
    item.metadata = {
        "hostile_count": 1,
        "esi_resolution": {
            "attempted": True,
            "character_name_count": 0,
            "resolved_character_count": 0,
            "system_name_matched": False,
        },
    }

    event = ScoringEngine(cooldown_seconds=0).score(item)

    assert event is None


def test_blacklist_match_raises_event_to_critical():
    engine = ScoringEngine(
        watchlist=Watchlist(blacklist={"alice"}),
        cooldown_seconds=0,
    )

    event = engine.score(observation())

    assert event is not None
    assert event.score == 120
    assert event.level == "critical"
    assert evidence_types(event) == ["local_ocr_seen", "blacklist_match"]


def test_blacklist_match_can_still_alert_when_local_ocr_source_is_suppressed():
    engine = ScoringEngine(
        watchlist=Watchlist(blacklist={"alice"}),
        cooldown_seconds=0,
    )

    event = engine.score(observation(character_ids=[], confidence=0.2))

    assert event is not None
    assert event.score == 80
    assert event.level == "high"
    assert evidence_types(event) == ["blacklist_match"]


def test_whitelist_suppresses_all_whitelisted_names():
    engine = ScoringEngine(
        watchlist=Watchlist(whitelist={"Alice", "Bob"}),
        cooldown_seconds=0,
    )

    assert engine.score(observation(names=["Alice", "Bob"])) is None


def test_whitelist_suppresses_ocr_leading_i_l_confusion():
    engine = ScoringEngine(
        watchlist=Watchlist(whitelist={"Iona Gonemion"}),
        cooldown_seconds=0,
    )

    assert engine.score(observation(names=["lona Gonemion"])) is None


def test_friendly_corporation_profile_suppresses_event():
    engine = ScoringEngine(
        watchlist=Watchlist(friendly_corporation_ids={42}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "corporation_id": "42"},
    )

    assert event is None


def test_friendly_alliance_profile_suppresses_event():
    engine = ScoringEngine(
        watchlist=Watchlist(friendly_alliance_ids={77}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "alliance_id": "77"},
    )

    assert event is None


def test_friendly_contact_standing_suppresses_event():
    engine = ScoringEngine(cooldown_seconds=0)

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "contact_standing": 5.0},
    )

    assert event is None


def test_friendly_contact_standing_can_be_disabled():
    engine = ScoringEngine(
        watchlist=Watchlist(friendly_standing_threshold=None),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"character_id": 123, "contact_standing": 10.0},
    )

    assert event is not None
    assert evidence_types(event) == ["intel_channel_report"]


def test_mixed_friendly_and_unknown_profiles_still_alerts():
    engine = ScoringEngine(
        watchlist=Watchlist(friendly_corporation_ids={42}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel", names=["Alice", "Bob"]),
        character_profiles=[
            {"character_id": 123, "corporation_id": "42"},
            {"character_id": 456, "corporation_id": "99"},
        ],
    )

    assert event is not None
    assert evidence_types(event) == ["intel_channel_report"]


def test_hostile_corporation_profile_adds_evidence():
    engine = ScoringEngine(
        watchlist=Watchlist(hostile_corporation_ids={42}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profile={"corporation_id": "42"},
    )

    assert event is not None
    assert event.score == 90
    assert event.level == "high"
    assert evidence_types(event) == ["intel_channel_report", "hostile_corporation"]


def test_intel_channel_metadata_enriches_evidence_summary():
    item = observation(source="intel_channel")
    item.names = []
    item.metadata = {
        "hostile_count": 3,
        "jump_count": 2,
        "direction": "Oijanen",
    }

    event = ScoringEngine(cooldown_seconds=0).score(item)

    assert event is not None
    assert event.evidence[0].summary == (
        "Intel channel reported 3 hostiles in Tama toward Oijanen (2 jumps)"
    )


def test_hostile_standing_profile_adds_evidence():
    event = ScoringEngine(cooldown_seconds=0).score(
        observation(source="intel_channel"),
        character_profile={"standing": -10.0},
    )

    assert event is not None
    assert event.score == 100
    assert event.level == "critical"
    assert evidence_types(event) == ["intel_channel_report", "hostile_standing"]


def test_neutral_standing_profile_adds_hostile_evidence_by_default():
    event = ScoringEngine(cooldown_seconds=0).score(
        observation(source="intel_channel"),
        character_profile={"contact_standing": 0.0},
    )

    assert event is not None
    assert evidence_types(event) == ["intel_channel_report", "hostile_standing"]


def test_recent_kill_activity_is_ignored_when_killboard_is_disabled():
    activity = SimpleNamespace(character_id=123, window="7d", kills=5)
    event = ScoringEngine(cooldown_seconds=0).score(
        observation(source="intel_channel"),
        kill_activity=activity,
    )

    assert event is not None
    assert event.score == 30
    assert event.level == "low"
    assert evidence_types(event) == ["intel_channel_report"]


def test_group_kill_activity_is_ignored_when_killboard_is_disabled():
    activity = SimpleNamespace(
        entity_type="corporation",
        entity_id=456,
        window="7d",
        kills=12,
        losses=2,
    )

    event = ScoringEngine(cooldown_seconds=0).score(
        observation(source="intel_channel"),
        group_activity=activity,
    )

    assert event is not None
    assert event.score == 30
    assert event.level == "low"
    assert evidence_types(event) == ["intel_channel_report"]


def test_recent_channel_mentions_do_not_alert_local_ocr_without_hostile_evidence():
    same_system = Observation(
        source="intel_channel",
        system_name="Tama",
        raw_text="Scout A: Tama +3 reds",
        seen_at="2026-06-29T11:58:00+00:00",
    )
    adjacent_system = Observation(
        source="intel_channel",
        system_name="Oijanen",
        raw_text="Scout B: Oijanen Some Pilot",
        seen_at="2026-06-29T11:40:00+00:00",
    )

    event = ScoringEngine(cooldown_seconds=0).score(
        observation(source="local_ocr"),
        channel_mentions=[
            ChannelMention(same_system, relation="same_system", age_seconds=120),
            ChannelMention(
                adjacent_system,
                relation="adjacent_system",
                age_seconds=1200,
            ),
        ],
    )

    assert event is None


def test_recent_channel_mentions_add_context_after_hostile_evidence():
    same_system = Observation(
        source="intel_channel",
        system_name="Tama",
        raw_text="Scout A: Tama +3 reds",
        seen_at="2026-06-29T11:58:00+00:00",
    )
    adjacent_system = Observation(
        source="intel_channel",
        system_name="Oijanen",
        raw_text="Scout B: Oijanen Some Pilot",
        seen_at="2026-06-29T11:40:00+00:00",
    )
    engine = ScoringEngine(
        watchlist=Watchlist(hostile_alliance_ids={99}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="local_ocr"),
        character_profile={"alliance_id": 99},
        channel_mentions=[
            ChannelMention(same_system, relation="same_system", age_seconds=120),
            ChannelMention(
                adjacent_system,
                relation="adjacent_system",
                age_seconds=1200,
            ),
        ],
    )

    assert event is not None
    assert event.score == 145
    assert event.level == "critical"
    assert evidence_types(event) == [
        "local_ocr_seen",
        "hostile_alliance",
        "intel_channel_same_system_recent",
        "intel_channel_adjacent_system_recent",
    ]
    assert event.evidence[2].summary == (
        "Recent intel channel mention 2 minutes ago in Tama"
    )
    assert event.evidence[3].summary == (
        "Recent intel channel mention 20 minutes ago in adjacent system Oijanen"
    )


def test_multiple_enrichment_items_add_profile_evidence_without_killboard_bonus():
    engine = ScoringEngine(
        watchlist=Watchlist(hostile_alliance_ids={99}),
        cooldown_seconds=0,
    )

    event = engine.score(
        observation(source="intel_channel"),
        character_profiles=[
            {"corporation_id": 42},
            {"alliance_id": 99},
        ],
        kill_activities=[
            SimpleNamespace(character_id=123, window="7d", kills=5),
        ],
    )

    assert event is not None
    assert event.score == 90
    assert event.level == "high"
    assert evidence_types(event) == [
        "intel_channel_report",
        "hostile_alliance",
    ]


def test_cooldown_suppresses_repeat_alerts_for_same_system_and_names():
    clock = [1000.0]
    engine = ScoringEngine(cooldown_seconds=60, now=lambda: clock[0])
    item = observation(source="intel_channel")

    assert engine.score(item) is not None
    assert engine.score(item) is None

    clock[0] = 1061.0

    assert engine.score(item) is not None


def test_reset_cooldown_allows_immediate_reentry_alert():
    engine = ScoringEngine(cooldown_seconds=60, now=lambda: 1000.0)
    item = observation(source="intel_channel")

    assert engine.score(item) is not None
    assert engine.score(item) is None
    assert engine.reset_cooldown(item.system_name, item.names) is True
    assert engine.score(item) is not None


def test_scoring_replay_records_version_and_predictable_rule_ids():
    base = observation(source="intel_channel")
    default_event = ScoringEngine(cooldown_seconds=0).score(base)
    blacklist_event = ScoringEngine(
        watchlist=Watchlist(blacklist={"alice"}),
        cooldown_seconds=0,
    ).score(base)

    assert default_event is not None
    assert blacklist_event is not None
    assert default_event.scoring_version == SCORING_VERSION
    assert blacklist_event.scoring_version == SCORING_VERSION
    assert default_event.score == 30
    assert blacklist_event.score == 110
    assert [item.rule_id for item in default_event.evidence] == [
        "intel_channel_report"
    ]
    assert [item.rule_id for item in blacklist_event.evidence] == [
        "intel_channel_report",
        "blacklist_match",
    ]
