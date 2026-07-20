import pytest
from pydantic import ValidationError

from eve_risk.config import Settings


def test_documented_zkill_user_agent_format_is_valid() -> None:
    settings = Settings(
        _env_file=None,
        zkill_user_agent="EveRiskAnalysis/0.1 Maintainer: Example <ops@example.org>",
    )
    settings.require_zkill()


def test_placeholder_zkill_contact_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            zkill_user_agent="EveRiskAnalysis/0.1 Maintainer: name <email@example.com>",
        )


def test_friendly_entity_ids_accept_common_separators() -> None:
    settings = Settings(
        _env_file=None,
        friendly_character_ids="1001, 1002，1003;1002",
        friendly_corporation_ids="2001",
        friendly_alliance_ids="3001,3002",
    )

    assert settings.friendly_character_id_set == {1001, 1002, 1003}
    assert settings.friendly_corporation_id_set == {2001}
    assert settings.friendly_alliance_id_set == {3001, 3002}
