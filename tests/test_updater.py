import hashlib
import json
from pathlib import Path

import pytest

from app.updater import (
    UpdateError,
    build_update_script,
    is_newer_version,
    parse_release_manifest,
)


def release_payload(**overrides):
    payload = {
        "version": "1.2.0",
        "url": "https://download.example/EVE-Sentry-Monitor-ONNX-1.2.0.zip",
        "sha256": hashlib.sha256(b"release").hexdigest(),
        "size": 7,
        "filename": "EVE-Sentry-Monitor-ONNX-1.2.0.zip",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("candidate", "installed", "expected"),
    [
        ("1.0.1", "1.0.0", True),
        ("v1.1", "1.0.9", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0-rc1", "1.0.0", False),
        ("2.0.0", "10.0.0", False),
    ],
)
def test_is_newer_version(candidate, installed, expected):
    assert is_newer_version(candidate, installed) is expected


def test_parse_release_manifest_validates_required_fields():
    release = parse_release_manifest(release_payload())

    assert release.version == "1.2.0"
    assert release.size == 7
    assert release.filename.endswith(".zip")


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "http://download.example/client.zip"},
        {"sha256": "bad"},
        {"size": 0},
        {"filename": "../client.zip"},
        {"version": "latest"},
    ],
)
def test_parse_release_manifest_rejects_unsafe_values(overrides):
    with pytest.raises(UpdateError):
        parse_release_manifest(release_payload(**overrides))


def test_build_update_script_waits_replaces_and_restarts(tmp_path):
    package = tmp_path / "client's-update.zip"
    install = Path("C:/Apps/EVE Sentry")

    script = build_update_script(package, install, "EVE-Sentry-Monitor.exe", 4242)

    assert "Wait-Process -Id 4242" in script
    assert "robocopy $source $install /MIR" in script
    assert "Start-Process -FilePath" in script
    assert "client''s-update.zip" in script


def test_release_manifest_example_is_json_serializable():
    rendered = json.dumps(release_payload())
    assert json.loads(rendered)["version"] == "1.2.0"
