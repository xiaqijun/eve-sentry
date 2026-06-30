from app.core.models import Observation
from app.intel.scoring import ScoringEngine, Watchlist
from app.killboard.analyzer import GroupKillActivity, KillActivity


def observation(source="local_ocr", names=None):
    return Observation(
        source=source,
        system_name="Tama",
        names=list(names or ["Alice"]),
        character_ids=[123],
        raw_text="Tama Alice",
        seen_at="2026-06-29T12:00:00+00:00",
        received_at="2026-06-29T12:00:01+00:00",
        observation_id=f"obs-{source}",
    )


def evidence_types(event):
    return [item.evidence_type for item in event.evidence]


def test_local_ocr_scores_medium_with_evidence():
    event = ScoringEngine(cooldown_seconds=0).score(observation())

    assert event is not None
    assert event.score == 40
    assert event.level == "medium"
    assert event.names == ["Alice"]
    assert evidence_types(event) == ["local_ocr_seen"]


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


def test_whitelist_suppresses_all_whitelisted_names():
    engine = ScoringEngine(
        watchlist=Watchlist(whitelist={"Alice", "Bob"}),
        cooldown_seconds=0,
    )

    assert engine.score(observation(names=["Alice", "Bob"])) is None


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


def test_recent_kill_activity_adds_bonus_evidence():
    activity = KillActivity(character_id=123, window="7d", kills=5)
    event = ScoringEngine(cooldown_seconds=0).score(
        observation(source="intel_channel"),
        kill_activity=activity,
    )

    assert event is not None
    assert event.score == 50
    assert event.level == "medium"
    assert evidence_types(event) == ["intel_channel_report", "recent_kill_activity"]


def test_group_kill_activity_adds_conservative_bonus_evidence():
    activity = GroupKillActivity(
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
    assert event.score == 45
    assert event.level == "medium"
    assert evidence_types(event) == [
        "intel_channel_report",
        "corporation_kill_activity",
    ]
    assert event.evidence[1].summary == (
        "Corporation 456 has 12 recent kills from zKillboard and 2 losses"
    )


def test_multiple_enrichment_items_add_profile_and_kill_evidence():
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
            KillActivity(character_id=123, window="7d", kills=5),
        ],
    )

    assert event is not None
    assert event.score == 110
    assert event.level == "critical"
    assert evidence_types(event) == [
        "intel_channel_report",
        "hostile_alliance",
        "recent_kill_activity",
    ]


def test_cooldown_suppresses_repeat_alerts_for_same_system_and_names():
    clock = [1000.0]
    engine = ScoringEngine(cooldown_seconds=60, now=lambda: clock[0])

    assert engine.score(observation()) is not None
    assert engine.score(observation()) is None

    clock[0] = 1061.0

    assert engine.score(observation()) is not None
