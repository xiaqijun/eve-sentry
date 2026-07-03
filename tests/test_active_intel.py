from app.core.active_intel import (
    ActiveIntelItem,
    channel_ttl_seconds,
    contains_clear_signal,
)


def test_active_intel_item_serializes_realtime_fields():
    item = ActiveIntelItem(
        active_id="ocr:client:s-kswl:alice",
        source="eve-sentry-detector",
        source_instance="client",
        system_name="S-KSWL",
        target_type="character",
        name="Alice",
        first_seen_at="2026-07-03T10:00:00+00:00",
        last_seen_at="2026-07-03T10:00:02+00:00",
        active=True,
        seen_count=2,
        source_observation_ids=["obs-1"],
    )

    assert item.to_dict()["id"] == "ocr:client:s-kswl:alice"
    assert item.to_dict()["active"] is True
    assert item.to_dict()["seen_count"] == 2
    assert item.to_dict()["source_observation_ids"] == ["obs-1"]


def test_contains_clear_signal_matches_english_and_chinese_words():
    assert contains_clear_signal("Tama clr")
    assert contains_clear_signal("Oijanen clear")
    assert contains_clear_signal("S-KSWL 清了")
    assert contains_clear_signal("本地安全")
    assert not contains_clear_signal("Tama +3 reds")


def test_contains_clear_signal_does_not_match_clear_inside_words():
    assert not contains_clear_signal("clearance reported")


def test_active_intel_item_to_dict_returns_shallow_copies():
    item = ActiveIntelItem(
        active_id="ocr:client:s-kswl:alice",
        source="eve-sentry-detector",
        source_instance="client",
        system_name="S-KSWL",
        metadata={"client_id": "client"},
        source_observation_ids=["obs-1"],
    )

    serialized = item.to_dict()
    serialized["metadata"]["client_id"] = "other"
    serialized["source_observation_ids"].append("obs-2")

    assert item.metadata == {"client_id": "client"}
    assert item.source_observation_ids == ["obs-1"]


def test_active_intel_item_default_collections_are_not_shared():
    first = ActiveIntelItem(
        active_id="ocr:client:s-kswl:alice",
        source="eve-sentry-detector",
        source_instance="client",
        system_name="S-KSWL",
    )
    second = ActiveIntelItem(
        active_id="ocr:client:s-kswl:bob",
        source="eve-sentry-detector",
        source_instance="client",
        system_name="S-KSWL",
    )

    first.metadata["client_id"] = "client"
    first.source_observation_ids.append("obs-1")

    assert second.metadata == {}
    assert second.source_observation_ids == []


def test_channel_ttl_seconds_uses_expected_defaults():
    assert channel_ttl_seconds({"hostile_count": 3}) == 180
    assert channel_ttl_seconds({"jump_count": 1}) == 300
    assert channel_ttl_seconds({"fleet": True}) == 900
    assert channel_ttl_seconds({"bridge": True}) == 1200
