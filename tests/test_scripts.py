import json
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_monitor_ui_smoke_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/monitor_ui_smoke.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "monitor client UI smoke check" in result.stdout


def test_monitor_ui_smoke_constructs_main_window_offscreen_without_side_effects():
    result = subprocess.run(
        [sys.executable, "scripts/monitor_ui_smoke.py", "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["qt_platform"] == "offscreen"
    assert payload["window_title"] == "EVE Sentry"
    assert payload["minimum_size"] == [860, 560]
    assert payload["main_window_created"] is True
    assert payload["intel_client_created"] is False
    assert payload["heartbeat_timer_active"] is False
    assert payload["channel_timer_active"] is False
    assert payload["monitoring"] is False
    assert payload["worker_count"] == 0
    assert payload["window_combo_count"] == 0
    assert payload["window_label"] == "Window: not found"
    assert payload["monitor_button"] == "Start Monitor"
    assert payload["status_card_keys"] == [
        "server",
        "esi",
        "ocr",
        "channel",
        "window",
        "region",
    ]
    assert payload["style_applied"] is True
    assert payload["runtime_files_created"] == []
    assert payload["side_effects"] == {
        "activate_window_calls": 0,
        "capturer_close_calls": 1,
        "capturer_created": 1,
        "get_window_info_calls": 0,
        "intel_client_created": 0,
        "list_eve_windows_calls": 1,
        "network_requests": 0,
        "ocr_created": 1,
        "ocr_recognize_calls": 0,
        "screenshot_calls": 0,
        "select_window_calls": 0,
        "tray_setup_patched": True,
    }


def test_import_intel_json_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/import_intel_json.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Import legacy JSON intel reports" in result.stdout


def test_run_server_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/run_server.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Run the intel server" in result.stdout


def test_start_alert_client_powershell_script_wraps_alert_client():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/start_alert_client.ps1",
            "-PrintCommand",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["args"][:2] == ["-m", "app.alert_client"]
    assert payload["cwd"].endswith("eve-sentry")
    assert "--popup" in payload["args"]
    assert "--details" in payload["args"]
    assert "--state" in payload["args"]
    state = payload["args"][payload["args"].index("--state") + 1]
    assert "EVE Sentry" in state


def test_start_alert_client_powershell_script_maps_optional_flags():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/start_alert_client.ps1",
            "-PrintCommand",
            "-Server",
            "http://example.invalid",
            "-State",
            "custom_state.json",
            "-NoPopup",
            "-NoDetails",
            "-MinLevel",
            "high",
            "-Ack",
            "-AckBy",
            "operator",
            "-UnacknowledgedOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    args = payload["args"]
    assert args[args.index("--server") + 1] == "http://example.invalid"
    assert args[args.index("--state") + 1] == "custom_state.json"
    assert "--popup" not in args
    assert "--details" not in args
    assert args[args.index("--min-level") + 1] == "high"
    assert args[args.index("--ack-by") + 1] == "operator"
    assert "--unacknowledged-only" in args


def test_start_monitor_client_powershell_script_wraps_detector_client():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/start_monitor_client.ps1",
            "-PrintCommand",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["args"] == ["-m", "app.detector_client"]
    assert payload["cwd"].endswith("eve-sentry")
    assert payload["env"]["EVE_SENTRY_INTEL_URL"] == "http://127.0.0.1:8765"
    assert "EVE Sentry" in payload["env"]["EVE_SENTRY_CHANNEL_STATE"]
    assert payload["env"]["EVE_SENTRY_HEARTBEAT_INTERVAL"] == "15"


def test_start_monitor_client_powershell_script_maps_runtime_options():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/start_monitor_client.ps1",
            "-PrintCommand",
            "-Server",
            "http://example.invalid",
            "-Channel",
            "wc.Venal+Br+Te",
            "-ChatlogDir",
            "C:\\EVE\\Chatlogs",
            "-ChannelState",
            "custom_offsets.json",
            "-System",
            "Tama",
            "-OcrDevice",
            "cpu",
            "-HeartbeatInterval",
            "20",
            "-NoPublish",
            "-NoEsiLocation",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    env = payload["env"]
    assert env["EVE_SENTRY_INTEL_URL"] == "http://example.invalid"
    assert env["EVE_SENTRY_CHANNEL"] == "wc.Venal+Br+Te"
    assert env["EVE_SENTRY_CHATLOG_DIR"] == "C:\\EVE\\Chatlogs"
    assert env["EVE_SENTRY_CHANNEL_STATE"] == "custom_offsets.json"
    assert env["EVE_SENTRY_SYSTEM"] == "Tama"
    assert env["EVE_SENTRY_OCR_DEVICE"] == "cpu"
    assert env["EVE_SENTRY_HEARTBEAT_INTERVAL"] == "20"
    assert env["EVE_SENTRY_PUBLISH_INTEL"] == "0"
    assert env["EVE_SENTRY_USE_ESI_LOCATION"] == "0"


def test_run_server_builds_argv_from_environment():
    module = _load_script_module("run_server", "scripts/run_server.py")

    argv = module.build_server_argv(
        {
            "EVE_SENTRY_SERVER_HOST": "0.0.0.0",
            "EVE_SENTRY_SERVER_PORT": "9000",
            "EVE_SENTRY_SERVER_STORAGE": "sqlite",
            "EVE_SENTRY_SERVER_DB": "/srv/eve/intel.sqlite3",
            "EVE_SENTRY_SERVER_CONFIG": "/srv/eve/intel_config.json",
            "EVE_SENTRY_SERVER_MAP_CONFIG": "/srv/eve/intel_map.json",
            "EVE_SENTRY_SERVER_MAP_SOURCE": "sde",
            "EVE_SENTRY_SERVER_MAP_SDE_PATH": "/srv/eve/sde/3417089",
            "EVE_SENTRY_SERVER_MAP_REGION_IDS": "10000045,10000033",
            "EVE_SENTRY_SERVER_MAP_SYSTEM_IDS": "30003617",
            "EVE_SENTRY_SERVER_MAP_REFRESH_ON_START": "1",
            "EVE_SENTRY_SERVER_ENABLE_ESI": "1",
            "EVE_SENTRY_SERVER_ESI_CACHE": "/srv/eve/esi_cache.json",
            "EVE_SENTRY_SERVER_ESI_CLIENT_ID": "client-id",
            "EVE_SENTRY_SERVER_ESI_TOKEN_FILE": "/srv/eve/esi_tokens.json",
            "EVE_SENTRY_SERVER_ESI_TOKEN_STORAGE": "plain",
            "EVE_SENTRY_SERVER_ESI_SCOPES": (
                "esi-location.read_location.v1,esi-characters.read_contacts.v1"
            ),
            "EVE_SENTRY_SERVER_ENABLE_KILLBOARD": "true",
            "EVE_SENTRY_SERVER_ZKILL_CACHE": "/srv/eve/zkill_cache.json",
        }
    )

    assert argv == [
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
        "--storage",
        "sqlite",
        "--db",
        "/srv/eve/intel.sqlite3",
        "--config",
        "/srv/eve/intel_config.json",
        "--map-config",
        "/srv/eve/intel_map.json",
        "--map-source",
        "sde",
        "--map-sde-path",
        "/srv/eve/sde/3417089",
        "--map-region",
        "10000045",
        "--map-region",
        "10000033",
        "--map-system",
        "30003617",
        "--map-refresh-on-start",
        "--enable-esi",
        "--esi-cache",
        "/srv/eve/esi_cache.json",
        "--esi-client-id",
        "client-id",
        "--esi-token-file",
        "/srv/eve/esi_tokens.json",
        "--esi-token-storage",
        "plain",
        "--esi-scope",
        "esi-location.read_location.v1",
        "--esi-scope",
        "esi-characters.read_contacts.v1",
        "--enable-killboard",
        "--zkill-cache",
        "/srv/eve/zkill_cache.json",
    ]


def test_run_server_main_appends_cli_args(monkeypatch):
    module = _load_script_module("run_server", "scripts/run_server.py")
    recorded = []

    def fake_main(argv):
        recorded.append(list(argv))
        return 7

    monkeypatch.setattr(module.server_main, "main", fake_main)
    monkeypatch.setattr(
        module,
        "build_server_argv",
        lambda env=None: ["--host", "127.0.0.1", "--port", "8765"],
    )

    assert module.main(["--enable-killboard"]) == 7
    assert recorded == [
        ["--host", "127.0.0.1", "--port", "8765", "--enable-killboard"]
    ]


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


def _load_script_module(name: str, relative_path: str):
    path = Path(relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
