from app.intel.classification import CLASSIFICATION_VERSION, ClassificationEngine
from app.intel.config import IntelConfigStore, ScoringConfig


def test_scoring_config_normalizes_payload_and_builds_scorer():
    config = ScoringConfig.from_payload(
        {
            "whitelist": [" Alice ", "alice", ""],
            "blacklist": "Bob",
            "friendly_corporation_ids": ["24", "24", "bad"],
            "friendly_alliance_ids": [88],
            "hostile_corporation_ids": ["42", "42", "bad"],
            "hostile_alliance_ids": [77],
            "friendly_standing_threshold": "4.5",
            "hostile_standing_threshold": None,
            "cooldown_seconds": "0",
        }
    )
    scorer = config.build_scorer()

    assert config.whitelist == ["Alice"]
    assert config.blacklist == ["Bob"]
    assert config.friendly_corporation_ids == [24]
    assert config.friendly_alliance_ids == [88]
    assert config.hostile_corporation_ids == [42]
    assert config.hostile_alliance_ids == [77]
    assert config.friendly_standing_threshold == 4.5
    assert config.hostile_standing_threshold is None
    assert config.to_dict()["schema_version"] == "scoring_config.v1"
    assert config.to_dict()["scoring_version"] == CLASSIFICATION_VERSION
    assert config.to_dict()["classification_version"] == CLASSIFICATION_VERSION
    assert any(
        item["type"] == "blacklist_match"
        for item in config.to_dict()["evidence_rules"]
    )
    assert isinstance(scorer, ClassificationEngine)
    assert scorer.cooldown_seconds == 0
    assert scorer.watchlist.whitelist == {"Alice"}
    assert scorer.watchlist.friendly_corporation_ids == {24}
    assert scorer.watchlist.friendly_alliance_ids == {88}
    assert scorer.watchlist.friendly_standing_threshold == 4.5


def test_config_store_persists_partial_updates(tmp_path):
    path = tmp_path / "intel_config.json"
    store = IntelConfigStore(path)

    config = store.update(
        {
            "blacklist": ["Alice"],
            "hostile_standing_threshold": -10,
        }
    )
    reloaded = IntelConfigStore(path)

    assert config.blacklist == ["Alice"]
    assert reloaded.to_dict()["blacklist"] == ["Alice"]
    assert reloaded.to_dict()["hostile_standing_threshold"] == -10.0
    assert reloaded.to_dict()["cooldown_seconds"] == 60.0


def test_config_defaults_treat_neutral_standing_as_hostile():
    config = ScoringConfig.from_payload({})
    scorer = config.build_scorer()

    assert config.hostile_standing_threshold == 0.0
    assert config.to_dict()["defaults"]["hostile_standing_threshold"] == 0.0
    assert scorer.watchlist.hostile_standing_threshold == 0.0
