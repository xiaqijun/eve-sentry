import hashlib
import base64
import json
import os
from pathlib import Path

import pytest

from app.updater import (
    UpdateError,
    build_update_script,
    cleanup_update_artifacts,
    canonical_manifest_bytes,
    is_newer_version,
    parse_release_manifest,
    verify_release_manifest_signature,
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


def test_parse_release_manifest_accepts_separate_model_component():
    model_payload = release_payload(
        version="model-2026-07",
        filename="EVE-Sentry-Monitor-ONNX-models-model-2026-07.zip",
        url="https://download.example/EVE-Sentry-Monitor-ONNX-models-model-2026-07.zip",
    )
    payload = release_payload(components={"models": model_payload})

    release = parse_release_manifest(payload)

    assert release.models is not None
    assert release.models.version == "model-2026-07"
    assert release.models.filename.endswith(".zip")


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
    assert "previous-version" in script
    assert "--update-health-marker" in script
    assert "previous version restored" in script


def test_release_manifest_signature_rejects_tampering(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "update_public_key.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    payload = release_payload(
        signature_algorithm="ed25519",
        signing_key_id="test",
    )
    payload["signature"] = base64.b64encode(
        private_key.sign(canonical_manifest_bytes(payload))
    ).decode("ascii")

    verify_release_manifest_signature(payload, public_key_path)

    payload["size"] += 1
    with pytest.raises(UpdateError):
        verify_release_manifest_signature(payload, public_key_path)


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
    for path in (package, script):
        os.utime(path, (0, 0))
    (stale_stage / "payload.bin").write_text("data", encoding="utf-8")
    os.utime(stale_stage, (0, 0))

    cleanup_update_artifacts(update_dir, temp_dir)

    assert not package.exists()
    assert partial.read_text(encoding="utf-8") == "data"
    assert not script.exists()
    assert not stale_stage.exists()
    assert active_stage.exists()
    assert unrelated.read_text(encoding="utf-8") == "data"


def test_cleanup_update_artifacts_removes_empty_update_directory(tmp_path):
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    script = update_dir / "apply-1.2.0.ps1"
    script.write_text("data", encoding="utf-8")
    os.utime(script, (0, 0))

    cleanup_update_artifacts(update_dir, tmp_path / "temp")

    assert not update_dir.exists()


def test_release_manifest_example_is_json_serializable():
    rendered = json.dumps(release_payload())
    assert json.loads(rendered)["version"] == "1.2.0"
