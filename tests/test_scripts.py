import json
import subprocess
import sys


def test_live_ocr_probe_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/live_ocr_probe.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Probe the live EVE window" in result.stdout


def test_channel_smoke_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/channel_smoke.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "channel-intel smoke test" in result.stdout


def test_channel_smoke_posts_sample_chatlog_to_local_server(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    log_path = log_dir / "Alliance Intel_20260630_120000.txt"
    log_path.write_text(
        "\n".join(
            [
                "Listener: Alliance Intel",
                "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
                "[ 2026.06.30 12:02:44 ] Scout B > Some Pilot in Oijanen",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/channel_smoke.py",
            "--log-dir",
            str(log_dir),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["posted"] == 2
    assert payload["observation_count"] == 2
    assert payload["alert_count"] == 2
    assert any(
        item["metadata"].get("hostile_count") == 3
        for item in payload["observations"]
    )
