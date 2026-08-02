import hashlib
import base64
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.updater import (
    ClientUpdater,
    ReleaseInfo,
    UpdateError,
    build_update_script,
    cleanup_update_artifacts,
    canonical_manifest_bytes,
    configured_update_proxy,
    is_newer_version,
    load_pending_update,
    load_update_result,
    network_proxy_from_url,
    parse_release_manifest,
    save_pending_update,
    validate_install_preflight,
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


def write_program_package(path: Path, content: bytes = b"program") -> ReleaseInfo:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "EVE-Sentry-Monitor-ONNX/EVE-Sentry-Monitor.exe",
            content,
        )
    data = path.read_bytes()
    return ReleaseInfo(
        "1.2.0",
        "https://download.example/client.zip",
        hashlib.sha256(data).hexdigest(),
        len(data),
        path.name,
    )


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


def test_configured_update_proxy_prefers_eve_sentry_setting(monkeypatch):
    for name in (
        "EVE_SENTRY_HTTP_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://system.example:8080")
    monkeypatch.setenv("EVE_SENTRY_HTTP_PROXY", "http://user:pass@proxy.example:7890")

    assert configured_update_proxy() == "http://user:pass@proxy.example:7890"


def test_network_proxy_from_url_supports_http_and_socks5():
    http_proxy = network_proxy_from_url("http://user:pass@proxy.example:7890")
    assert http_proxy is not None
    assert http_proxy.type().name == "HttpProxy"
    assert http_proxy.hostName() == "proxy.example"
    assert http_proxy.port() == 7890
    assert http_proxy.user() == "user"
    assert http_proxy.password() == "pass"

    socks_proxy = network_proxy_from_url("socks5://proxy.example")
    assert socks_proxy is not None
    assert socks_proxy.type().name == "Socks5Proxy"
    assert socks_proxy.port() == 1080


def test_network_proxy_from_url_rejects_unsupported_or_invalid_values():
    assert network_proxy_from_url("") is None
    assert network_proxy_from_url("ftp://proxy.example:21") is None
    assert network_proxy_from_url("http://:7890") is None


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

    script = build_update_script(
        package,
        install,
        "EVE-Sentry-Monitor.exe",
        4242,
        target_version="1.2.0",
        package_sha256="a" * 64,
        package_size=123,
    )

    assert "Wait-Process -Id 4242" in script
    assert "robocopy $source $install /MIR" in script
    assert "Start-Process -FilePath" in script
    assert "client''s-update.zip" in script
    assert "EVE-Sentry-Monitor-*.zip" not in script
    assert "*.zip.part" not in script
    assert "apply-*.ps1" not in script
    assert "previous-version" in script
    assert "--update-health-marker" in script
    assert "$confirmedVersion -eq $targetVersion" in script
    assert "Stop-Process -Id $newProcess.Id" in script
    assert "$backupComplete -and $installStarted" in script
    assert "Move-Item -LiteralPath $temporaryResult" in script
    assert "8-second stability confirmation" in script
    assert "Get-FileHash -LiteralPath $package" in script
    assert "$expectedPackageSize = [int64]123" in script
    assert "a" * 64 in script


def test_client_updater_reports_verified_package_ready_to_install(tmp_path):
    updater = ClientUpdater(
        update_dir=tmp_path / "updates",
        background_download=False,
    )

    assert updater.ready_to_install is False
    updater._ready_path = tmp_path / "program.zip"
    assert updater.ready_to_install is True

    updater._installer_launched = True
    assert updater.ready_to_install is False


def test_pending_update_is_restored_after_client_restart(tmp_path):
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    package = update_dir / "EVE-Sentry-Monitor-ONNX-1.2.0.zip"
    release = write_program_package(package)
    save_pending_update(update_dir, release, package, None)

    updater = ClientUpdater(
        installed_version="1.1.0",
        update_dir=update_dir,
        background_download=False,
    )

    assert updater.ready_to_install is True
    assert updater._release is not None
    assert updater._release.version == release.version
    assert updater._release.sha256 == release.sha256
    assert updater._release.size == release.size
    assert updater._ready_path == package


def test_pending_update_rejects_tampered_package(tmp_path):
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    package = update_dir / "EVE-Sentry-Monitor-ONNX-1.2.0.zip"
    release = write_program_package(package)
    save_pending_update(update_dir, release, package, None)
    package.write_bytes(b"x" * release.size)

    assert load_pending_update(update_dir, "1.1.0") is None
    assert not (update_dir / "pending-update.json").exists()


def test_inflight_target_keeps_pending_package_available_after_rollback(tmp_path):
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    package = update_dir / "EVE-Sentry-Monitor-ONNX-1.2.0.zip"
    release = write_program_package(package)
    save_pending_update(update_dir, release, package, None)
    (update_dir / "apply-1.2.0.ps1").write_text("running", encoding="utf-8")
    os.utime(package, (0, 0))

    target_client = ClientUpdater(
        installed_version="1.2.0",
        update_dir=update_dir,
        background_download=False,
    )

    assert target_client.ready_to_install is False
    assert package.exists()
    assert (update_dir / "pending-update.json").exists()

    rolled_back_client = ClientUpdater(
        installed_version="1.1.0",
        update_dir=update_dir,
        background_download=False,
    )
    assert rolled_back_client.ready_to_install is True
    assert rolled_back_client._ready_path == package


def test_update_result_is_consumed_once(tmp_path):
    result_path = tmp_path / "update-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "rolled_back",
                "version": "1.2.0",
                "message": "restored",
            }
        ),
        encoding="utf-8",
    )

    assert load_update_result(tmp_path) == {
        "status": "rolled_back",
        "version": "1.2.0",
        "message": "restored",
    }
    assert load_update_result(tmp_path) is None


def test_client_updater_surfaces_previous_install_result(tmp_path):
    updater = ClientUpdater(update_dir=tmp_path, background_download=False)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "update-result.json").write_text(
        json.dumps(
            {
                "status": "success",
                "version": "1.0.9",
                "message": "客户端更新成功",
            }
        ),
        encoding="utf-8",
    )
    states = []
    updater.state_changed.connect(lambda *state: states.append(state))

    updater._emit_initial_update_state()

    assert states == [("客户端更新成功", "检查更新", True)]
    assert not (tmp_path / "update-result.json").exists()


def test_install_preflight_checks_archive_and_disk_space(tmp_path, monkeypatch):
    install_dir = tmp_path / "client"
    install_dir.mkdir()
    (install_dir / "old.bin").write_bytes(b"old")
    package = tmp_path / "program.zip"
    write_program_package(package)

    validate_install_preflight(install_dir, [package])

    monkeypatch.setattr(
        "app.updater.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1),
    )
    with pytest.raises(UpdateError, match="磁盘空间不足"):
        validate_install_preflight(install_dir, [package])


def test_install_preflight_rejects_package_without_executable(tmp_path):
    install_dir = tmp_path / "client"
    install_dir.mkdir()
    package = tmp_path / "program.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("readme.txt", "missing executable")

    with pytest.raises(UpdateError, match="缺少 EVE-Sentry-Monitor.exe"):
        validate_install_preflight(install_dir, [package])


def test_installer_preflight_failure_does_not_launch_or_request_exit(
    tmp_path,
    monkeypatch,
):
    install_dir = tmp_path / "client"
    install_dir.mkdir()
    executable = install_dir / "EVE-Sentry-Monitor.exe"
    executable.write_bytes(b"old")
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    package = update_dir / "program.zip"
    package.write_bytes(b"not a zip")
    release = ReleaseInfo(
        "1.2.0",
        "https://download.example/program.zip",
        hashlib.sha256(package.read_bytes()).hexdigest(),
        package.stat().st_size,
        package.name,
    )
    updater = ClientUpdater(update_dir=update_dir, background_download=False)
    updater._release = release
    updater._ready_path = package
    updater._program_ready_path = package
    states = []
    restarts = []
    updater.state_changed.connect(lambda *state: states.append(state))
    updater.restart_requested.connect(lambda: restarts.append(True))
    monkeypatch.setattr("app.updater.sys.platform", "win32")
    monkeypatch.setattr("app.updater.sys.executable", str(executable))
    monkeypatch.setattr("app.updater.sys.frozen", True, raising=False)
    monkeypatch.setattr(
        "app.updater.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PowerShell launched")
        ),
    )

    updater.install_and_restart()

    assert updater.ready_to_install is True
    assert restarts == []
    assert states[-1][0].startswith("安装前检查失败：")
    assert states[-1][1:] == ("重试安装", True)


def test_installer_rechecks_ready_package_hash_before_shutdown(tmp_path, monkeypatch):
    install_dir = tmp_path / "client"
    install_dir.mkdir()
    executable = install_dir / "EVE-Sentry-Monitor.exe"
    executable.write_bytes(b"old")
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    package = update_dir / "program.zip"
    release = write_program_package(package)
    tampered = bytearray(package.read_bytes())
    tampered[-1] ^= 1
    package.write_bytes(tampered)
    updater = ClientUpdater(update_dir=update_dir, background_download=False)
    updater._release = release
    updater._ready_path = package
    updater._program_ready_path = package
    states = []
    updater.state_changed.connect(lambda *state: states.append(state))
    monkeypatch.setattr("app.updater.sys.platform", "win32")
    monkeypatch.setattr("app.updater.sys.executable", str(executable))
    monkeypatch.setattr("app.updater.sys.frozen", True, raising=False)
    monkeypatch.setattr(
        "app.updater.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PowerShell launched")
        ),
    )

    updater.install_and_restart()

    assert updater.ready_to_install is True
    assert "SHA256 校验失败" in states[-1][0]


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="PowerShell unavailable")
def test_generated_installer_script_parses_in_windows_powershell(
    tmp_path,
    monkeypatch,
):
    script_path = tmp_path / "apply.ps1"
    script_path.write_text(
        build_update_script(
            tmp_path / "client's update.zip",
            Path("C:/Apps/EVE Sentry"),
            "EVE-Sentry-Monitor.exe",
            4242,
            target_version="1.2.0",
        ),
        encoding="utf-8-sig",
    )
    command = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:EVE_SENTRY_TEST_SCRIPT,[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
    )
    monkeypatch.setenv("EVE_SENTRY_TEST_SCRIPT", str(script_path))

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


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


def test_cleanup_removes_owned_expanded_residue_and_preserves_pending_package(
    tmp_path,
):
    update_dir = tmp_path / "updates"
    update_dir.mkdir()
    expanded = update_dir / "EVE-Sentry-Monitor-ONNX"
    expanded.mkdir()
    (expanded / "old.exe").write_bytes(b"old")
    package = update_dir / "EVE-Sentry-Monitor-ONNX-1.2.0.zip"
    package.write_bytes(b"verified")
    os.utime(package, (0, 0))

    cleanup_update_artifacts(
        update_dir,
        tmp_path / "temp",
        preserved_paths=(package,),
    )

    assert not expanded.exists()
    assert package.read_bytes() == b"verified"


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
