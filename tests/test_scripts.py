import json
import subprocess
import sys

from app.server.intel_store import IntelStore
from app.server.sqlite_store import SQLiteIntelStore


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


def test_import_intel_json_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/import_intel_json.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Import legacy JSON intel reports" in result.stdout


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


def test_import_intel_json_dry_run_does_not_create_database(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    db_path = tmp_path / "intel.sqlite3"
    store = IntelStore(json_path, systems={}, links=[])
    store.add_report(
        "Tama",
        ["Alice"],
        source="ocr",
        seen_at="2026-06-29T12:00:00+00:00",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/import_intel_json.py",
            "--source",
            str(json_path),
            "--db",
            str(db_path),
            "--dry-run",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["source_count"] == 1
    assert payload["imported_count"] == 0
    assert not db_path.exists()


def test_import_intel_json_populates_sqlite_and_preserves_ack(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    db_path = tmp_path / "intel.sqlite3"
    store = IntelStore(json_path, systems={}, links=[])
    report = store.add_report(
        "Tama",
        ["Alice"],
        source="ocr",
        seen_at="2026-06-29T12:00:00+00:00",
    )
    store.ack_alert(f"evt_{report.report_id}", acknowledged_by="client", note="sent")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/import_intel_json.py",
            "--source",
            str(json_path),
            "--db",
            str(db_path),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["imported_count"] == 1
    assert payload["final_count"] == 1

    imported = SQLiteIntelStore(db_path, systems={}, links=[])
    alert = imported.list_alerts()[0]
    assert alert["source_observation_id"] == report.report_id
    assert alert["acknowledged"] is True
    assert alert["acknowledged_by"] == "client"
    assert alert["acknowledgement_note"] == "sent"


def test_import_intel_json_refuses_existing_database_without_replace(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    db_path = tmp_path / "intel.sqlite3"
    legacy = IntelStore(json_path, systems={}, links=[])
    legacy.add_report("Tama", ["Alice"], source="ocr")
    existing = SQLiteIntelStore(db_path, systems={}, links=[])
    existing.add_report("Oijanen", ["Bob"], source="manual")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/import_intel_json.py",
            "--source",
            str(json_path),
            "--db",
            str(db_path),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "already contains reports" in payload["error"]
    assert len(SQLiteIntelStore(db_path, systems={}, links=[]).list_reports()) == 1
