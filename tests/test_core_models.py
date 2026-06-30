from app.core.models import Observation, ThreatEvent, threat_level


def test_observation_from_payload_accepts_legacy_and_canonical_fields():
    observation = Observation.from_payload(
        {
            "system": " Tama ",
            "names": [" Alice ", "Bob", "Alice"],
            "source": "local_ocr",
            "system_id": "30002813",
            "character_ids": ["123", "123", "bad", 456],
            "note": "window=EVE",
            "seen_at": "2026-06-30T01:00:00+00:00",
        }
    )

    observation.validate()

    assert observation.system_name == "Tama"
    assert observation.names == ["Alice", "Bob"]
    assert observation.system_id == 30002813
    assert observation.character_ids == [123, 456]
    assert observation.raw_text == "window=EVE"


def test_threat_event_from_observation_includes_score_level_and_evidence():
    observation = Observation(
        source="local_ocr",
        system_name="Tama",
        names=["Alice"],
        seen_at="2026-06-30T01:00:00+00:00",
        received_at="2026-06-30T01:00:01+00:00",
        observation_id="obs-1",
    )

    event = ThreatEvent.from_observation(observation).to_dict()

    assert event["id"] == "evt_obs-1"
    assert event["score"] == 40
    assert event["level"] == "medium"
    assert event["system_name"] == "Tama"
    assert event["names"] == ["Alice"]
    assert event["evidence"][0]["type"] == "local_ocr_observed"


def test_threat_level_boundaries():
    assert threat_level(20) == "low"
    assert threat_level(40) == "medium"
    assert threat_level(70) == "high"
    assert threat_level(100) == "critical"

