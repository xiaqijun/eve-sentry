import hashlib
import json
import os
from pathlib import Path

import pytest

from app.updater import (
    UpdateError,
    build_update_script,
    cleanup_update_artifacts,
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
    assert "EVE-Sentry-Monitor-*.zip" in script
    assert "*.zip.part" in script
    assert "apply-*.ps1" in script
    assert "Remove-Item -LiteralPath $updateRoot" in script


def test_cleanup_update_artifacts_removes_only_owned_files(tmp_path):
    update_dir = tmp_path / "updates"
    temp_dir = tmp_path / "temp"
    update_dir.mkdir()
    temp_dir.mkdir()
    package = update_dir / "EVE-Sentry-Monitor-ONNX-1.2.0.zip"
    partial = update_dir / "EVE-Sentry-Monitor-ONNX-1.2.1.zip.part"
    script = update_dir / "apply-1.2.0.ps1"
    unrelated = update_dir / "keep-me.txt"
    stale_stage = temp_dir / "eve-sentry-update-stale"
    active_stage = temp_dir / "eve-sentry-update-active"
    stale_stage.mkdir()
    active_stage.mkdir()
    for path in (package, partial, script, unrelated):
        path.write_text("data", encoding="utf-8")
    (stale_stage / "payload.bin").write_text("data", encoding="utf-8")
    os.utime(stale_stage, (0, 0))

    cleanup_update_artifacts(update_dir, temp_dir)

    assert not package.exists()
    assert not partial.exists()
    assert not script.exists()
    assert not stale_stage.exists()
    assert active_stage.exists()
    assert unrelated.read_text(encoding="utf-8") == "data"


def test_cleanup_update_artifacts_removes_empty_update_directory(tmp_path):
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    (update_dir / "apply-1.2.0.ps1").write_text("data", encoding="utf-8")

    cleanup_update_artifacts(update_dir, tmp_path / "temp")

    assert not update_dir.exists()


def test_release_manifest_example_is_json_serializable():
    rendered = json.dumps(release_payload())
    assert json.loads(rendered)["version"] == "1.2.0"
