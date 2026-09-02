import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="deployment script targets Linux")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPOSITORY_ROOT / "deploy" / "ci" / "deploy_esi_gateway.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _fake_commands(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_LOG\"\n",
    )
    _write_executable(fake_bin / "journalctl", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        "if [[ \"${FAKE_HEALTHY:-true}\" == true ]]; then\n"
        "  printf '{\"ok\": true}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 22\n",
    )
    return fake_bin


def _create_archive(tmp_path: Path, revision: str, unit_marker: str) -> tuple[Path, str]:
    payload = tmp_path / f"payload-{revision}"
    backend = payload / "backend"
    (backend / "esi_gateway").mkdir(parents=True)
    (backend / "scripts").mkdir()
    (backend / "deploy" / "linux").mkdir(parents=True)
    (backend / "esi_gateway" / "client.py").write_text("# client\n", encoding="utf-8")
    (backend / "scripts" / "esi_gateway.py").write_text("# gateway\n", encoding="utf-8")
    (backend / "deploy" / "linux" / "eve-sentry-esi-gateway.service").write_text(
        f"# {unit_marker}\n", encoding="utf-8"
    )
    (backend / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (payload / "manifest.json").write_text(
        json.dumps(
            {
                "artifact": "eve-sentry-esi-gateway",
                "revision": revision,
                "layout_version": 2,
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / f"{revision}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for child in payload.iterdir():
            stream.add(child, arcname=child.name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, checksum


def _deploy(
    tmp_path: Path,
    gateway_root: Path,
    unit_dir: Path,
    revision: str,
    *,
    healthy: bool,
    unit_marker: str,
) -> subprocess.CompletedProcess[str]:
    archive, checksum = _create_archive(tmp_path, revision, unit_marker)
    fake_bin = tmp_path / "bin"
    runtime_bin = gateway_root / ".venv" / "bin"
    runtime_bin.mkdir(parents=True, exist_ok=True)
    runtime_python = runtime_bin / "python"
    if not runtime_python.exists():
        runtime_python.symlink_to(sys.executable)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "SYSTEMD_UNIT_DIR": str(unit_dir),
            "DEPLOY_HEALTH_ATTEMPTS": "1",
            "DEPLOY_HEALTH_DELAY": "0",
            "FAKE_HEALTHY": str(healthy).lower(),
            "FAKE_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        }
    )
    return subprocess.run(
        [
            "bash",
            str(DEPLOY_SCRIPT),
            str(archive),
            revision,
            checksum,
            str(gateway_root),
            "eve-sentry-esi-gateway",
            "http://127.0.0.1:8787/health",
            "3",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_deployer_activates_verified_release(tmp_path: Path) -> None:
    fake_bin = _fake_commands(tmp_path)
    assert fake_bin.is_dir()
    gateway_root = tmp_path / "gateway"
    unit_dir = tmp_path / "systemd"
    revision = "1" * 40

    result = _deploy(
        tmp_path,
        gateway_root,
        unit_dir,
        revision,
        healthy=True,
        unit_marker="release-one",
    )

    assert result.returncode == 0, result.stderr
    assert (gateway_root / "current").resolve() == gateway_root / "releases" / revision
    assert (gateway_root / "deployed-revision").read_text(encoding="utf-8").strip() == revision
    assert "release-one" in (unit_dir / "eve-sentry-esi-gateway.service").read_text(encoding="utf-8")


def test_deployer_rolls_back_release_and_unit_when_health_fails(tmp_path: Path) -> None:
    _fake_commands(tmp_path)
    gateway_root = tmp_path / "gateway"
    unit_dir = tmp_path / "systemd"
    first_revision = "1" * 40
    second_revision = "2" * 40
    first = _deploy(
        tmp_path,
        gateway_root,
        unit_dir,
        first_revision,
        healthy=True,
        unit_marker="release-one",
    )
    assert first.returncode == 0, first.stderr

    second = _deploy(
        tmp_path,
        gateway_root,
        unit_dir,
        second_revision,
        healthy=False,
        unit_marker="release-two",
    )

    assert second.returncode == 1
    assert "restoring the previous release" in second.stderr
    assert (gateway_root / "current").resolve() == gateway_root / "releases" / first_revision
    assert (gateway_root / "deployed-revision").read_text(encoding="utf-8").strip() == first_revision
    assert "release-one" in (unit_dir / "eve-sentry-esi-gateway.service").read_text(encoding="utf-8")
