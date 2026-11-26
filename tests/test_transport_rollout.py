"""Configuration edits preserve unrelated settings, and rollback on failure."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "transport_rollout",
    Path(__file__).parents[1] / "deploy/ci/configure_esi_transport.py",
)
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


def test_mode_edit_preserves_other_settings_and_removes_duplicate_mode_lines():
    original = '# retained\nTOKEN="secret with spaces"\nMODE=legacy\nMODE=dual\n'
    updated = rollout.replace_mode(original, "MODE", "relay")
    assert updated == '# retained\nTOKEN="secret with spaces"\nMODE=relay\n'
    assert rollout.environment(updated) == {
        "TOKEN": "secret with spaces",
        "MODE": "relay",
    }


def test_preflight_uses_saved_credential_and_rejects_a_different_gateway(tmp_path, monkeypatch):
    from app.esi.transport import TransportEsiClient

    startup = "http://10.1.2.3:8787"
    alternative = "http://10.1.2.4:8787"
    token = "updated-gateway-token-" + "a" * 20
    override = tmp_path / "intel.esi-gateway.json"
    checked = []

    def get_system(client, system_id):
        checked.append(client.connections.relay_target)
        return {"name": "Jita"}

    monkeypatch.setattr(TransportEsiClient, "get_system", get_system)
    env = {
        "EVE_SENTRY_SERVER_CONFIG": str(tmp_path / "intel.json"),
        "EVE_SENTRY_SERVER_ESI_GATEWAY_URL": startup,
        "EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN": "z" * 32,
        "EVE_SENTRY_SERVER_ESI_GATEWAY_ALLOWED_URLS": alternative,
    }
    for url in (startup, alternative):
        override.write_text(json.dumps({"url": url, "token": token, "revision": "saved"}), encoding="utf-8")
        override.chmod(0o600)
        if url == startup:
            rollout.preflight("server", "relay", env)
        else:
            with pytest.raises(RuntimeError, match="deployment target"):
                rollout.preflight("server", "relay", env)
    assert checked == [("10.1.2.3", 8787, token)]


@pytest.mark.skipif(
    os.name != "posix", reason="systemd rollout uses POSIX locks and file permissions"
)
def test_restart_failure_restores_original_configuration(tmp_path, monkeypatch):
    env = tmp_path / "server.env"
    env.write_text("TOKEN=retained\nMODE=legacy\n")
    original = env.read_bytes()
    monkeypatch.setitem(
        rollout.TARGETS,
        "server",
        (env, "fake-service", "MODE", "http://localhost/ready", tmp_path / "lock"),
    )
    monkeypatch.setattr(rollout, "health", lambda *args, **kwargs: None)
    monkeypatch.setattr(rollout, "preflight", lambda *args: None)
    calls = []

    def restart(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("injected restart failure")

    monkeypatch.setattr(rollout.subprocess, "run", restart)
    with pytest.raises(RuntimeError, match="injected"):
        rollout.switch("server", "relay", apply=True)
    assert env.read_bytes() == original
    assert len(calls) == 2
    assert next(tmp_path.glob("server.env.transport-backup-*")).read_bytes() == original
