"""Exercise the deployment SDE gate without touching services or the network."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("sync_status,check_status,expected", [(0, 1, 0), (124, 0, 0), (1, 0, 0), (124, 1, 1), (1, 1, 1)])
def test_deploy_sde_deadline_and_fallback(tmp_path, sync_status, check_status, expected):
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = "C:/Program Files/Git/bin/bash.exe"
    if not bash or not Path(bash).is_file():
        pytest.skip("bash is required for the deployment shell regression")
    script = (Path(__file__).resolve().parents[1] / "scripts/ci/deploy_release.sh").read_text()
    function = script.split("prepare_sde() {", 1)[1].split("\n}\n", 1)[0]
    harness = "prepare_sde() {" + function + "\n}\n" + f'''
set -euo pipefail
database_url=unused
redis_url=unused
data_dir=unused
timeout() {{
    printf 'TIMEOUT_ARGS %s\\n' "$*"
    if [[ "${{!#}}" == "--check-only" ]]; then
        return {check_status}
    fi
    return {sync_status}
}}
prepare_sde .
'''
    result = subprocess.run([bash, "-c", harness], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stdout + result.stderr
    assert "TIMEOUT_ARGS --kill-after=10s 180s env" in result.stdout
    if sync_status:
        assert "TIMEOUT_ARGS --kill-after=5s 15s env" in result.stdout
        assert "--check-only" in result.stdout
    else:
        assert "--check-only" not in result.stdout
