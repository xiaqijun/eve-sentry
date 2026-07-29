import os
import subprocess
import sys
from pathlib import Path


def test_update_signing_cli_imports_app_outside_repository(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    script = repository_root / "scripts" / "update_signing.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "prepare-public" in completed.stdout
    assert "sign" in completed.stdout
