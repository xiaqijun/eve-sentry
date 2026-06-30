from app.intel.config import IntelConfigStore, ScoringConfig


def test_scoring_config_normalizes_payload_and_builds_scorer():
    config = ScoringConfig.from_payload(
        {
            "whitelist": [" Alice ", "alice", ""],
            "blacklist": "Bob",
            "hostile_corporation_ids": ["42", "42", "bad"],
            "hostile_alliance_ids": [77],
            "hostile_standing_threshold": None,
            "cooldown_seconds": "0",
        }
    )
    scorer = config.build_scorer()

    assert config.whitelist == ["Alice"]
    assert config.blacklist == ["Bob"]
    assert config.hostile_corporation_ids == [42]
    assert config.hostile_alliance_ids == [77]
    assert config.hostile_standing_threshold is None
    assert scorer.cooldown_seconds == 0
    assert scorer.watchlist.whitelist == {"Alice"}


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
