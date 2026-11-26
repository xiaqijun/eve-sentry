"""Quickstarts share a local token and launch the existing Relay service."""

import os
import runpy
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_QUICKSTART = ROOT / "scripts/quickstart.py"
DOCKER_QUICKSTART = ROOT / "deploy/docker/quickstart.sh"
GATEWAY = "10.2.3.4"
SENTRY = "10.2.3.5"
TOKEN_KEY = "EVE_SENTRY_ESI_GATEWAY_TOKEN"


def test_python_starts_relay_with_local_token(tmp_path, monkeypatch, capsys):
    helpers = runpy.run_path(str(PYTHON_QUICKSTART))
    main = helpers["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["quickstart.py", GATEWAY, SENTRY])
    launched = []

    def launch(args, *, env):
        launched.append((args, env))
        return 0

    monkeypatch.setattr(subprocess, "call", launch)
    assert main() == 0
    assert main() == 0
    (args, environment), (_, repeated) = launched
    token = environment[TOKEN_KEY]
    assert args[-2:] == ["--transport-mode", "relay"]
    assert len(token) == 64 and token == repeated[TOKEN_KEY]
    assert token not in " ".join(args) and token not in capsys.readouterr().out
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "gateway.env").stat().st_mode) == 0o600

    monkeypatch.setattr(sys, "argv", ["quickstart.py", "10.2.3.6", SENTRY])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and len(launched) == 2
    assert token in (tmp_path / "gateway.env").read_text(encoding="utf-8")


def test_docker_starts_with_python_quickstart_token(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is required for the Docker launcher test")
    directory = tmp_path / "deploy/docker"
    directory.mkdir(parents=True)
    script = directory / "quickstart.sh"
    shutil.copyfile(DOCKER_QUICKSTART, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*"\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}

    invalid = subprocess.run([bash, str(script), "0.0.0.0", SENTRY], env=env, capture_output=True, text=True)
    assert invalid.returncode == 2 and not (tmp_path / "gateway.env").exists()
    first = subprocess.run([bash, str(script), GATEWAY, SENTRY], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "compose up -d --build --wait" in first.stdout
    config = tmp_path / "gateway.env"
    original = config.read_bytes()
    helpers = runpy.run_path(str(PYTHON_QUICKSTART))
    token = helpers["gateway_config"](config, GATEWAY, SENTRY)[TOKEN_KEY]
    second = subprocess.run([bash, str(script), GATEWAY, SENTRY], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert config.read_bytes() == original and token not in (first.stdout + second.stdout)
