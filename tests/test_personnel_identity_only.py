"""Personnel resolution never requests combat statistics."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.models import Observation
from app.esi.session import ContactStanding
from app.intel.classification import ClassificationEngine
from app.intel.enrichment import ThreatEnricher
from app.server import __main__ as server_main
from scripts.run_server import build_server_argv


@pytest.mark.parametrize("single_profile", [False, True])
def test_identity_and_hostile_standing_do_not_query_killboard(single_profile):
    profile = {
        "character_id": 123,
        "name": "Pilot One",
        "corporation_id": 456,
        "corporation_name": "Example Corp",
        "alliance_id": 789,
        "alliance_name": "Example Alliance",
        "zkill_url": "https://zkillboard.com/character/123/",
    }
    resolver = SimpleNamespace(character_profile=Mock(return_value=profile))
    killboard = Mock()
    session = SimpleNamespace(snapshot=Mock(return_value=SimpleNamespace(
        contacts=[ContactStanding(
            contact_id=456, contact_type="corporation", standing=-10,
        )],
    )))
    enricher = ThreatEnricher(
        resolver=resolver, killboard=killboard, esi_session=session,
    )
    observation = Observation(
        source="eve-sentry-detector", system_name="Tama",
        names=["Pilot One"], character_ids=[123, 123],
    )

    profiles = (
        [enricher.character_profile(123)] if single_profile
        else enricher.enrich(observation).character_profiles
    )

    assert len(profiles) == 1
    assert profiles[0] == {
        **profile,
        "contact_standing": -10.0,
        "standing_source": "esi_contacts",
        "standing_contact_id": 456,
        "standing_contact_type": "corporation",
    }
    assert "zkill" not in profiles[0]
    assert "zkill_danger_ratio" not in profiles[0]
    assert killboard.mock_calls == []
    resolver.character_profile.assert_called_once_with(123)
    # Retiring statistics must not retire the enemy/friendly classification.
    result = ClassificationEngine().classify(
        observation, observation.names, profiles,
    )
    assert result is not None and result.classification == "red"


@pytest.mark.parametrize("flags", [[], ["--enable-killboard"], ["--disable-killboard"]])
def test_server_startup_never_constructs_a_killboard(monkeypatch, tmp_path, flags):
    from app.intel import zkillboard

    killboard_factory = Mock(side_effect=AssertionError("retired network client"))
    monkeypatch.setattr(zkillboard, "ZkillboardClient", killboard_factory)
    monkeypatch.setattr(server_main, "_build_public_esi_client", lambda args: object())
    monkeypatch.setattr(server_main.MapConfigStore, "build_map", lambda *a, **kw: ({}, []))
    captured = {}

    class StartupChecked(Exception):
        pass

    def capture_store(args, **kwargs):
        captured.update(kwargs)
        raise StartupChecked

    monkeypatch.setattr(server_main, "_build_store", capture_store)
    with pytest.raises(StartupChecked):
        server_main.main([
            "--storage", "json", "--enable-esi",
            "--config", str(tmp_path / "config.json"),
            "--map-config", str(tmp_path / "map.json"),
            "--esi-cache", str(tmp_path / "esi.json"),
            *flags,
        ])

    killboard_factory.assert_not_called()
    assert captured["resolver"] is not None
    assert isinstance(captured["enricher"], ThreatEnricher)
    assert isinstance(captured["scorer"], ClassificationEngine)


def test_retired_deployment_flags_cannot_reenable_statistics():
    argv = build_server_argv({
        "EVE_SENTRY_SERVER_ENABLE_ESI": "1",
        "EVE_SENTRY_SERVER_ENABLE_ZKILL": "1",
        "EVE_SENTRY_SERVER_DISABLE_ZKILL": "1",
    })
    assert "--enable-esi" in argv
    assert "--enable-killboard" not in argv
    assert "--disable-killboard" not in argv
    help_text = server_main.build_arg_parser().format_help()
    assert "killboard" not in help_text
