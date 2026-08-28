import json
import base64
import importlib.util
import io
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.server.intel_store import IntelStore
from app.server.http_server import IntelHTTPServer


def eve_chat_timestamp(offset_seconds: int = 0) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=offset_seconds)
    ).strftime("%Y.%m.%d %H:%M:%S")


def test_live_ocr_probe_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/live_ocr_probe.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Probe the live EVE window" in result.stdout
    assert "--diagnose-windows" in result.stdout
    assert "--diagnose-only" in result.stdout


def test_live_ocr_probe_classifies_window_diagnostics_without_real_eve():
    module = _load_script_module(
        "live_ocr_probe",
        "scripts/live_ocr_probe.py",
    )

    class FakeWin32Gui:
        hwnds = [1, 2, 3, 4, 5]
        titles = {
            1: "EVE - Active",
            2: "EVE - Minimized",
            3: "EVE Launcher",
            4: "EVE - Hidden",
            5: "Chrome",
        }
        rects = {
            1: (10, 20, 1290, 740),
            2: (-32000, -32000, -32000, -32000),
            3: (40, 50, 1320, 770),
            4: (70, 80, 1350, 800),
            5: (0, 0, 800, 600),
        }
        client_rects = {
            1: (0, 0, 1280, 720),
            2: (0, 0, 0, 0),
            3: (0, 0, 1280, 720),
            4: (0, 0, 1280, 720),
            5: (0, 0, 800, 600),
        }
        visible = {1: True, 2: True, 3: True, 4: False, 5: True}
        iconic = {1: False, 2: True, 3: False, 4: False, 5: False}

        @classmethod
        def EnumWindows(cls, callback, param):
            for hwnd in cls.hwnds:
                callback(hwnd, param)

        @classmethod
        def GetWindowText(cls, hwnd):
            return cls.titles[hwnd]

        @classmethod
        def GetWindowThreadProcessId(cls, hwnd):
            raise AssertionError("use FakeWin32Process")

        @classmethod
        def IsWindowVisible(cls, hwnd):
            return cls.visible[hwnd]

        @classmethod
        def IsIconic(cls, hwnd):
            return cls.iconic[hwnd]

        @classmethod
        def GetWindowRect(cls, hwnd):
            return cls.rects[hwnd]

        @classmethod
        def GetClientRect(cls, hwnd):
            return cls.client_rects[hwnd]

    class FakeWin32Process:
        pids = {1: 101, 2: 102, 3: 103, 4: 104, 5: 105}

        @classmethod
        def GetWindowThreadProcessId(cls, hwnd):
            return (0, cls.pids[hwnd])

    class FakeProcess:
        names = {
            101: "exefile.exe",
            102: "exefile.exe",
            103: "exefile.exe",
            104: "exefile.exe",
            105: "chrome.exe",
        }

        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return self.names[self.pid]

    class FakePsutil:
        Process = FakeProcess

    diagnostics = module.collect_window_diagnostics(
        "EVE -",
        win32gui_module=FakeWin32Gui,
        win32process_module=FakeWin32Process,
        psutil_module=FakePsutil,
    )

    by_title = {item["title"]: item for item in diagnostics}
    assert set(by_title) == {
        "EVE - Active",
        "EVE - Minimized",
        "EVE Launcher",
        "EVE - Hidden",
    }
    assert by_title["EVE - Active"]["reason"] == "usable_candidate"
    assert by_title["EVE - Active"]["client_size"] == [1280, 720]
    assert by_title["EVE - Minimized"]["reason"] == "minimized"
    assert by_title["EVE Launcher"]["reason"] == "usable_candidate"
    assert by_title["EVE Launcher"]["title_match"] is False
    assert by_title["EVE Launcher"]["process_match"] is True
    assert by_title["EVE - Hidden"]["reason"] == "not_visible"
    assert "Chrome" not in by_title


def test_live_ocr_probe_diagnose_only_skips_ocr_capture_and_artifacts(
    monkeypatch,
    tmp_path,
):
    module = _load_script_module(
        "live_ocr_probe_diagnose_only",
        "scripts/live_ocr_probe.py",
    )
    calls = {
        "capture": 0,
        "close": 0,
        "ocr": 0,
        "select": 0,
    }

    class FakeCapturer:
        def list_eve_windows(self, keyword="EVE -"):
            assert keyword == "EVE -"
            return [
                {
                    "hwnd": 101,
                    "title": "EVE - Active",
                    "x": 0,
                    "y": 0,
                    "w": 1280,
                    "h": 720,
                }
            ]

        def select_window(self, *args, **kwargs):
            _ = args, kwargs
            calls["select"] += 1

        def screenshot(self, *args, **kwargs):
            _ = args, kwargs
            calls["capture"] += 1
            raise AssertionError("diagnose-only must not capture")

        def close(self):
            calls["close"] += 1

    class FakeOCREngine:
        def __init__(self, *args, **kwargs):
            _ = args, kwargs
            calls["ocr"] += 1
            raise AssertionError("diagnose-only must not initialize OCR")

    monkeypatch.setattr(module, "Capturer", FakeCapturer)
    monkeypatch.setattr(module, "OCREngine", FakeOCREngine)
    monkeypatch.setattr(
        module,
        "collect_window_diagnostics",
        lambda keyword: [
            {
                "hwnd": 101,
                "title": "EVE - Active",
                "reason": "usable_candidate",
                "title_match": True,
                "process_match": True,
            }
        ],
    )
    monkeypatch.chdir(tmp_path)

    assert module.main(["--diagnose-only"]) == 0
    assert calls == {"capture": 0, "close": 1, "ocr": 0, "select": 0}
    assert list(tmp_path.iterdir()) == []


def test_live_ocr_probe_stdout_fallback_handles_non_gbk_characters(monkeypatch):
    module = _load_script_module(
        "live_ocr_probe_stdout",
        "scripts/live_ocr_probe.py",
    )

    class GbkFailingStdout:
        def __init__(self):
            self.buffer = io.BytesIO()
            self.flushed = False

        def write(self, text):
            text.encode("gbk")
            return len(text)

        def flush(self):
            self.flushed = True

    fake_stdout = GbkFailingStdout()
    monkeypatch.setattr(module.sys, "stdout", fake_stdout)

    module._write_stdout("EVE - Pilot\u200b")

    assert fake_stdout.buffer.getvalue().decode("utf-8") == "EVE - Pilot\u200b\n"


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


def test_nginx_template_disables_buffering_for_v1_event_stream():
    config = Path("deploy/linux/eve-sentry.nginx.conf").read_text(encoding="utf-8")

    assert (
        "limit_req_zone $binary_remote_addr "
        "zone=eve_sentry_api:10m rate=20r/s;"
    ) in config
    assert (
        "limit_conn_zone $binary_remote_addr zone=eve_sentry_sse:10m;"
        in config
    )
    assert "client_max_body_size 1m;" in config
    assert "limit_req_status 429;" in config
    assert "limit_conn_status 429;" in config
    assert "location ~ ^/api/(v1/)?events$" in config
    event_location = config.split("location ~ ^/api/(v1/)?events$", 1)[1].split(
        "location /api/",
        1,
    )[0]
    assert "limit_req zone=eve_sentry_api burst=40 nodelay;" in event_location
    assert "limit_conn eve_sentry_sse 5;" in event_location
    assert "proxy_buffering off;" in event_location
    assert "proxy_cache off;" in event_location
    assert "proxy_read_timeout 3600s;" in event_location
    assert "proxy_set_header X-Request-ID $request_id;" in event_location

    api_location = config.split("location /api/", 1)[1].split("location /", 1)[0]
    assert "limit_req zone=eve_sentry_api burst=40 nodelay;" in api_location
    assert "limit_conn eve_sentry_sse" not in api_location
    assert "proxy_set_header X-Request-ID $request_id;" in api_location


def test_monitor_ui_smoke_constructs_main_window_offscreen_without_side_effects(
    tmp_path,
):
    screenshot_path = tmp_path / "monitor-ui-smoke.png"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/monitor_ui_smoke.py",
            "--json",
            "--screenshot",
            str(screenshot_path),
        ],
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
    assert payload["minimum_size"] == [900, 620]
    assert payload["main_window_created"] is True
    assert payload["intel_client_created"] is False
    assert payload["heartbeat_timer_active"] is False
    assert payload["monitoring"] is False
    assert payload["worker_count"] == 0
    assert payload["window_combo_count"] == 0
    assert payload["monitor_selection_text"] == "监控窗口 0/0"
    assert payload["monitor_selected_count"] == 0
    assert payload["monitor_offline_count"] == 0
    assert payload["monitor_selection_state"] == "empty"
    assert payload["monitor_selection_tooltip"] == "未检测到 EVE 窗口"
    assert payload["monitor_menu_items"] == []
    assert payload["window_label"] == "窗口：未找到"
    assert payload["window_status_rows"] == [
        [
            "-",
            "未知",
            "未检测到 EVE 窗口",
            "-",
            "未检测",
            "点击刷新或确认 EVE 已启动",
        ]
    ]
    assert payload["monitor_button"] == "开始监控"
    assert payload["status_card_keys"] == [
        "server",
        "esi",
        "ocr",
        "window",
        "region",
    ]
    assert payload["style_applied"] is True
    assert payload["theme_checks"] == {
        "app_qss_has_shell_colors": True,
        "inactive_button_style_applied": True,
        "inactive_button_style_has_accent": True,
        "active_button_style_has_danger": True,
        "all_status_cards_named": True,
        "all_status_cards_have_min_height": True,
        "all_status_cards_have_frame_style": True,
        "all_status_cards_have_transparent_label_style": True,
    }
    assert payload["monitor_button_style"] == {
        "inactive_applied": True,
        "inactive_contains": ["#0d5f75", "#23b7d8", "font-size: 13px"],
        "active_contains": ["#8f2f2d", "#d95752", "font-size: 13px"],
    }
    for key, detail in payload["status_card_details"].items():
        assert detail["object_name"] == f"status-card-{key}"
        assert detail["minimum_height"] >= 58
        assert detail["has_qframe_style"] is True
        assert detail["has_transparent_label_style"] is True
        assert detail["title"]
        assert detail["value"]
    assert payload["layout_checks"] == {
        "settings_width": 240,
        "settings_has_expected_width": True,
        "monitor_button_height": payload["layout_checks"]["monitor_button_height"],
        "monitor_button_has_readable_height": True,
        "right_controls_do_not_overlap": True,
    }
    assert payload["layout_checks"]["monitor_button_height"] >= 36
    assert payload["screenshot"]["captured"] is True
    assert payload["screenshot"]["path"] == str(screenshot_path)
    assert payload["screenshot"]["width"] >= 800
    assert payload["screenshot"]["height"] >= 540
    assert payload["screenshot"]["bytes"] > 1000
    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size == payload["screenshot"]["bytes"]
    assert payload["runtime_files_created"] == []
    assert payload["side_effects"] == {
        "activate_window_calls": 0,
        "capturer_close_calls": 1,
        "capturer_created": 1,
        "get_window_info_calls": 0,
        "heartbeat_posts": 0,
        "intel_client_created": 0,
        "list_eve_windows_calls": 1,
        "network_requests": 0,
        "ocr_created": 0,
        "ocr_snapshot_posts": 0,
        "ocr_recognize_calls": 0,
        "screenshot_calls": 0,
        "select_window_calls": 0,
        "tray_setup_patched": True,
    }


def test_monitor_ui_smoke_can_render_detected_window_offscreen():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/monitor_ui_smoke.py",
            "--json",
            "--fake-window",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["window_combo_count"] == 1
    assert payload["monitor_selection_text"] == "监控窗口 0/1"
    assert payload["monitor_selected_count"] == 0
    assert payload["monitor_offline_count"] == 0
    assert payload["monitor_selection_state"] == "empty"
    assert payload["monitor_selection_tooltip"] == "尚未选择监控窗口，请打开菜单进行勾选"
    assert payload["monitor_menu_items"] == [
        {
            "text": "Smoke Pilot · 未知 · 未监控",
            "checked": False,
            "online": True,
            "character": "Smoke Pilot",
            "system": "未知",
            "status": "未监控",
        }
    ]
    assert payload["window_combo_items"] == ["EVE - Smoke Pilot"]
    assert payload["selected_window"] == ""
    assert payload["window_label"] == "窗口：未选择"
    assert payload["window_status_rows"] == [
        [
            "Smoke Pilot",
            "Smoke",
            "EVE - Smoke Pilot",
            "-",
            "未选择",
            "未选择为监控窗口",
        ]
    ]
    assert payload["monitor_button"] == "开始监控"
    assert payload["layout_checks"]["right_controls_do_not_overlap"] is True
    assert payload["side_effects"]["select_window_calls"] == 0
    assert payload["side_effects"]["screenshot_calls"] == 0
    assert payload["side_effects"]["network_requests"] == 0


def test_monitor_ui_smoke_can_render_multiple_detected_windows_offscreen():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/monitor_ui_smoke.py",
            "--json",
            "--fake-window-count",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["window_combo_count"] == 2
    assert payload["monitor_selection_text"] == "监控窗口 0/2"
    assert payload["monitor_selected_count"] == 0
    assert payload["monitor_offline_count"] == 0
    assert payload["monitor_selection_state"] == "empty"
    assert payload["monitor_selection_tooltip"] == "尚未选择监控窗口，请打开菜单进行勾选"
    assert payload["monitor_menu_items"] == [
        {
            "text": "Smoke Pilot · 未知 · 未监控",
            "checked": False,
            "online": True,
            "character": "Smoke Pilot",
            "system": "未知",
            "status": "未监控",
        },
        {
            "text": "Smoke Pilot 2 · 未知 · 未监控",
            "checked": False,
            "online": True,
            "character": "Smoke Pilot 2",
            "system": "未知",
            "status": "未监控",
        },
    ]
    assert payload["window_combo_items"] == [
        "EVE - Smoke Pilot",
        "EVE - Smoke Pilot 2",
    ]
    assert payload["selected_window"] == ""
    assert payload["window_label"] == "窗口：未选择"
    assert payload["window_status_rows"] == [
        [
            "Smoke Pilot",
            "Smoke",
            "EVE - Smoke Pilot",
            "-",
            "未选择",
            "未选择为监控窗口",
        ],
        [
            "Smoke Pilot 2",
            "Smoke",
            "EVE - Smoke Pilot 2",
            "-",
            "未选择",
            "未选择为监控窗口",
        ],
    ]
    assert payload["status_card_values"]["window"] == "已选 0/2"
    assert payload["status_card_values"]["region"] == "未配置"
    assert payload["side_effects"]["select_window_calls"] == 0
    assert payload["side_effects"]["screenshot_calls"] == 0
    assert payload["side_effects"]["network_requests"] == 0


def test_integration_status_check_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/integration_status_check.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "without creating reports" in result.stdout
    assert "--output" in result.stdout
    assert "--expect-channel-monitoring" in result.stdout
    assert "--min-active-ocr-targets" in result.stdout
    assert "--expect-alert-mode" in result.stdout
    assert "--check-alert-detail" in result.stdout


def test_live_acceptance_bundle_help_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, "scripts/live_acceptance_bundle.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "read-only live EVE Sentry acceptance checks" in result.stdout
    assert "--expect-channel-monitoring" in result.stdout
    assert "--min-active-ocr-targets" in result.stdout
    assert "--expect-alert-mode" in result.stdout
    assert "--check-alert-detail" in result.stdout


def test_integration_status_check_classifies_client_heartbeats():
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )
    clients = {
        "heartbeats": [
            {
                "client_type": "detector_client",
                "status": "running",
                "stale": False,
                "details": {
                    "channel_monitoring": True,
                    "channels": ["wc.Venal+Br+Te"],
                    "targets": [
                        {"monitoring": True},
                        {"monitoring": True},
                    ]
                },
            },
            {
                "client_type": "alert_client",
                "status": "running",
                "stale": False,
                "details": {},
            },
        ]
    }

    grouped = module.classify_clients(clients)

    assert len(grouped["detector_client"]) == 1
    assert len(grouped["alert_client"]) == 1
    assert module.detector_target_count(grouped["detector_client"]) == 2
    assert module.detector_is_monitoring(grouped["detector_client"]) is True
    assert module.detector_channel_is_monitoring(grouped["detector_client"]) is True
    assert module.detector_channel_count(grouped["detector_client"]) == 1
    assert module.is_online(grouped["alert_client"][0]) is True


def test_integration_status_check_redacts_sensitive_evidence_fields():
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    payload = module.redact_sensitive(
        {
            "details": {
                "access_token": "token-value",
                "client_secret": "secret-value",
                "cookie": "session=value",
                "token_storage": "plain",
                "nested": {"refresh_token": "refresh-value"},
            },
            "esi": {
                "character_owner_hash": "owner-hash",
                "token_file": "/opt/eve-sentry/esi_tokens.json",
                "token_file_present": True,
            },
        }
    )

    assert payload["details"]["access_token"] == "[REDACTED]"
    assert payload["details"]["client_secret"] == "[REDACTED]"
    assert payload["details"]["cookie"] == "[REDACTED]"
    assert payload["details"]["nested"]["refresh_token"] == "[REDACTED]"
    assert payload["details"]["token_storage"] == "plain"
    assert payload["esi"]["character_owner_hash"] == "[REDACTED]"
    assert payload["esi"]["token_file"] == "[REDACTED]"
    assert payload["esi"]["token_file_present"] is True


def test_integration_status_check_event_stream_accepts_idle_sse(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class IdleSseResponse:
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def readline(self):
            raise TimeoutError("idle stream")

    monkeypatch.setattr(module, "urlopen", lambda request, timeout=0: IdleSseResponse())

    result = module.probe_event_stream("http://example.invalid", timeout=1)

    assert result == {
        "content_type": "text/event-stream; charset=utf-8",
        "sample": "",
        "ok": True,
    }


def test_integration_status_check_timeout_returns_structured_failure(monkeypatch, capsys):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    def timeout_urlopen(request, timeout=0):
        _ = request, timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(module, "urlopen", timeout_urlopen)

    result = module.main(["--server", "http://example.invalid", "--timeout", "0.1", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert result == 1
    assert output["ok"] is False
    assert output["read_only"] is True
    assert output["checks"] == [
        {
            "name": "connect",
            "ok": False,
            "detail": "GET http://example.invalid/api/health timed out after 0.1s",
        }
    ]
    assert output["evidence"]["write_endpoints_called"] == []


def test_integration_status_check_does_not_treat_stale_running_heartbeat_as_online():
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    assert module.is_online(
        {
            "status": "running",
            "age_seconds": 120.0,
            "stale_after_seconds": 45.0,
        }
    ) is False
    assert module.is_online(
        {
            "status": "running",
            "age_seconds": 10.0,
            "stale_after_seconds": 45.0,
        }
    ) is True


def test_integration_status_check_reads_empty_server_without_writing_intel(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    evidence_path = tmp_path / "evidence" / "integration-status.json"
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/integration_status_check.py",
                "--server",
                server.url,
                "--json",
                "--require-event-health",
                "--check-esi",
                "--check-map",
                "--check-events-stream",
                "--output",
                str(evidence_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.stop()

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["schema_version"] == "integration_status.v2"
    assert payload["summary"]["client_count"] == 0
    assert payload["summary"]["recent_alert_count"] == 0
    assert payload["summary"]["active_intel_count"] == 0
    assert payload["summary"]["active_ocr_count"] == 0
    assert payload["summary"]["active_channel_count"] == 0
    assert payload["summary"]["detector_channel_monitoring"] is False
    assert payload["summary"]["detector_channel_count"] == 0
    assert payload["detectors"] == []
    assert payload["alert_clients"] == []
    assert payload["channel_clients"] == []
    assert payload["active_ocr"] == []
    assert payload["active_channel"] == []
    assert payload["recent_alerts"] == []
    assert payload["health"]["schema_version"] == "health.v1"
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["active_intel_endpoint"]["ok"] is True
    assert checks["esi_status"]["ok"] is True
    assert checks["map_snapshot"]["ok"] is True
    assert checks["events_stream"]["ok"] is True
    assert evidence_path.exists()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["ok"] is True
    assert evidence["schema_version"] == "integration_status.v2"
    assert evidence["evidence"]["write_endpoints_called"] == []
    assert evidence["evidence"]["expected_conditions"]["event_health"] is True
    assert evidence["evidence"]["expected_conditions"]["detector_channel_monitoring"] is False
    assert any(url.endswith("/api/v1/clients") for url in evidence["evidence"]["checked_urls"])
    assert any("/api/v1/events?" in url for url in evidence["evidence"]["checked_urls"])
    endpoint_urls = [item["url"] for item in evidence["evidence"]["endpoints"]]
    assert any(url.endswith("/api/v1/clients") for url in endpoint_urls)
    assert any("/api/v1/active-intel?" in url for url in endpoint_urls)
    assert all(item["method"] == "GET" for item in evidence["evidence"]["endpoints"])


def test_integration_status_check_expect_detector_fails_without_client(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/integration_status_check.py",
                "--server",
                server.url,
                "--json",
                "--expect-detector",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.stop()

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["detector_online"]["ok"] is False


def test_integration_status_check_expect_alert_client_fails_without_client(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/integration_status_check.py",
                "--server",
                server.url,
                "--json",
                "--expect-alert-client",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.stop()

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["alert_client_online"]["ok"] is False


def test_integration_status_check_accepts_alert_client_runtime_overlay(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {
                "heartbeats": [
                    {
                        "client_id": "alert-client:test",
                        "client_type": "alert_client",
                        "status": "running",
                        "online": True,
                        "details": {
                            "mode": "events",
                            "transport": "events",
                            "popup": True,
                            "overlay": True,
                            "details": False,
                            "last_action": "connected",
                            "last_success_at": "2026-07-07T12:00:00+00:00",
                        },
                    }
                ]
            }

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--expect-alert-client",
                "--expect-alert-mode",
                "events",
                "--expect-alert-popup",
                "--expect-alert-overlay",
            ]
        )
    )

    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["alert_client_online"]["ok"] is True
    assert checks["alert_client_mode"]["ok"] is True
    assert checks["alert_client_mode"]["detail"] == "modes=['events'] expected=events"
    assert checks["alert_client_popup"]["ok"] is True
    assert checks["alert_client_overlay"]["ok"] is True
    assert payload["summary"]["alert_client_modes"] == ["events"]
    assert payload["summary"]["alert_client_popup"] is True
    assert payload["summary"]["alert_client_overlay"] is True
    assert payload["evidence"]["expected_conditions"]["alert_mode"] == "events"
    assert payload["evidence"]["expected_conditions"]["alert_popup"] is True
    assert payload["evidence"]["expected_conditions"]["alert_overlay"] is True


def test_integration_status_check_reads_recent_alert_detail_when_available(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {"heartbeats": []}

        def list_alerts(self, limit=5):
            return [{"id": "evt-1", "system_name": "S-KSWL"}]

        def alert_detail(self, alert_id):
            return {
                "alert": {"id": alert_id},
                "observation": {"source": "intel_channel"},
                "explanation": {"summary": "detail available"},
            }

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--check-alert-detail",
            ]
        )
    )

    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["alert_detail"]["ok"] is True
    assert checks["alert_detail"]["detail"] == "alert_id=evt-1"
    assert payload["recent_alert_detail"]["alert"]["id"] == "evt-1"
    assert payload["evidence"]["expected_conditions"]["alert_detail"] is True
    endpoint_urls = [item["url"] for item in payload["evidence"]["endpoints"]]
    assert "http://example.invalid/api/v1/alerts/evt-1" in endpoint_urls


def test_integration_status_check_skips_alert_detail_without_recent_alert(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {"heartbeats": []}

        def list_alerts(self, limit=5):
            return []

        def alert_detail(self, alert_id):
            raise AssertionError("alert_detail should not be called without alerts")

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--check-alert-detail",
            ]
        )
    )

    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["alert_detail"]["ok"] is True
    assert checks["alert_detail"]["detail"] == "skipped:no recent alerts"
    assert payload["recent_alert_detail"] is None


def test_integration_status_check_alert_client_runtime_mismatch_is_explicit(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {
                "heartbeats": [
                    {
                        "client_id": "alert-client:test",
                        "client_type": "alert_client",
                        "status": "running",
                        "online": True,
                        "details": {
                            "mode": "poll",
                            "transport": "poll",
                            "popup": False,
                            "overlay": False,
                            "details": False,
                        },
                    }
                ]
            }

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--expect-alert-mode",
                "events",
                "--expect-alert-popup",
                "--expect-alert-overlay",
            ]
        )
    )

    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["alert_client_mode"]["ok"] is False
    assert checks["alert_client_mode"]["detail"] == "modes=['poll'] expected=events"
    assert checks["alert_client_popup"]["detail"] == "popup=False"
    assert checks["alert_client_overlay"]["detail"] == "overlay=False"
    assert payload["summary"]["alert_client_modes"] == ["poll"]


def test_integration_status_check_expect_channel_client_fails_without_client(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/integration_status_check.py",
                "--server",
                server.url,
                "--json",
                "--expect-channel-client",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.stop()

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["channel_client_online"]["ok"] is False


def test_integration_status_check_expect_channel_client_accepts_online_heartbeat(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {
                "heartbeats": [
                    {
                        "client_id": "channel-client:test",
                        "client_type": "channel_client",
                        "status": "running",
                        "online": True,
                        "details": {"last_action": "server_parse:1"},
                    }
                ]
            }

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--expect-channel-client",
            ]
        )
    )

    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["channel_client_online"]["ok"] is True
    assert checks["channel_client_online"]["detail"] == "1 online channel client(s)"
    assert payload["summary"]["online_channel_client_count"] == 1


def test_integration_status_check_expect_channel_monitoring_fails_without_channel(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/integration_status_check.py",
                "--server",
                server.url,
                "--json",
                "--expect-channel-monitoring",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.stop()

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["detector_channel_monitoring"]["ok"] is False
    assert checks["detector_channel_monitoring"]["detail"] == "0 monitored channel(s)"


def test_integration_status_check_min_targets_failure_is_explicit(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {
                "heartbeats": [
                    {
                        "client_id": "detector-client:test",
                        "client_type": "detector_client",
                        "status": "running",
                        "online": True,
                        "details": {
                            "target_count": 1,
                            "targets": [{"monitoring": True}],
                        },
                    }
                ]
            }

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--expect-detector",
                "--expect-monitoring",
                "--min-targets",
                "2",
            ]
        )
    )

    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["detector_online"]["ok"] is True
    assert checks["detector_monitoring"]["ok"] is True
    assert checks["detector_targets"]["ok"] is False
    assert checks["detector_targets"]["detail"] == "highest target_count=1 expected>=2"
    assert payload["summary"]["detector_target_count"] == 1


def test_integration_status_check_counts_distinct_active_ocr_targets(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {"heartbeats": []}

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {
                "active_intel": [
                    {
                        "source": "eve-sentry-detector",
                        "source_instance": "EVE - Window A",
                        "system_name": "S-KSWL",
                        "name": "Pilot A",
                        "metadata": {"client_id": "detector-client:test:window-a"},
                    },
                    {
                        "source": "eve-sentry-detector",
                        "source_instance": "EVE - Window A",
                        "system_name": "S-KSWL",
                        "name": "Pilot B",
                        "metadata": {"client_id": "detector-client:test:window-a"},
                    },
                    {
                        "source": "eve-sentry-detector",
                        "source_instance": "EVE - Window B",
                        "system_name": "S-KSWL",
                        "name": "Pilot C",
                        "metadata": {"client_id": "detector-client:test:window-b"},
                    },
                    {
                        "source": "intel_channel",
                        "source_instance": "wc.Venal",
                        "system_name": "S-KSWL",
                        "name": "Pilot D",
                        "metadata": {},
                    },
                ]
            }

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--min-active-ocr-targets",
                "2",
            ]
        )
    )

    assert payload["ok"] is True
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["active_ocr_targets"]["ok"] is True
    assert checks["active_ocr_targets"]["detail"] == "2 active OCR target(s) expected>=2"
    assert payload["summary"]["active_ocr_count"] == 3
    assert payload["summary"]["active_ocr_target_count"] == 2
    assert payload["evidence"]["expected_conditions"]["min_active_ocr_targets"] == 2


def test_integration_status_check_min_active_ocr_targets_failure_is_explicit(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {"heartbeats": []}

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {
                "active_intel": [
                    {
                        "source": "eve-sentry-detector",
                        "source_instance": "EVE - Window A",
                        "system_name": "S-KSWL",
                        "name": "Pilot A",
                        "metadata": {"client_id": "detector-client:test:window-a"},
                    }
                ]
            }

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--min-active-ocr-targets",
                "2",
            ]
        )
    )

    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["active_ocr_targets"]["ok"] is False
    assert checks["active_ocr_targets"]["detail"] == "1 active OCR target(s) expected>=2"
    assert payload["summary"]["active_ocr_target_count"] == 1


def test_integration_status_check_ignores_stale_detector_monitoring(monkeypatch):
    module = _load_script_module(
        "integration_status_check",
        "scripts/integration_status_check.py",
    )

    class FakeApi:
        def __init__(self, _server, timeout=0):
            self.timeout = timeout

        def client_status(self):
            return {
                "heartbeats": [
                    {
                        "client_id": "detector-client:stale",
                        "client_type": "detector_client",
                        "status": "running",
                        "online": False,
                        "details": {
                            "monitoring": True,
                            "target_count": 2,
                            "targets": [
                                {"monitoring": True},
                                {"monitoring": True},
                            ],
                        },
                    },
                    {
                        "client_id": "detector-client:online-idle",
                        "client_type": "detector_client",
                        "status": "idle",
                        "online": True,
                        "details": {
                            "monitoring": False,
                            "target_count": 0,
                            "targets": [],
                        },
                    },
                ]
            }

        def list_alerts(self, limit=5):
            return []

        def get_active_intel(self, limit=50):
            return {"active_intel": []}

    monkeypatch.setattr(
        module,
        "fetch_json",
        lambda url, timeout: {"health": {"ok": True, "schema_version": "health.v1"}},
    )
    monkeypatch.setattr(module, "IntelApiClient", FakeApi)

    payload = module.build_status(
        module.parse_args(
            [
                "--server",
                "http://example.invalid",
                "--expect-detector",
                "--expect-monitoring",
                "--min-targets",
                "1",
            ]
        )
    )

    assert payload["ok"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["detector_online"]["ok"] is True
    assert checks["detector_monitoring"]["ok"] is False
    assert checks["detector_targets"]["ok"] is False
    assert payload["summary"]["online_detector_count"] == 1
    assert payload["summary"]["detector_monitoring"] is False
    assert payload["summary"]["detector_target_count"] == 0


def test_live_acceptance_bundle_writes_read_only_evidence_files(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    output_dir = tmp_path / "evidence" / "live"
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/live_acceptance_bundle.py",
                "--server",
                server.url,
                "--output-dir",
                str(output_dir),
                "--json",
                "--check-esi",
                "--check-map",
                "--check-events-stream",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.stop()

    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["ok"] is True
    assert manifest["schema_version"] == "live_acceptance_bundle.v1"
    assert manifest["read_only"] is True
    assert manifest["server"] == server.url
    assert manifest["write_endpoints_called"] == []
    assert "does not start clients" in manifest["notes"]
    assert len(manifest["files"]) == 3
    file_names = {item["name"] for item in manifest["files"]}
    assert file_names == {"baseline", "detector-channel", "alert-client"}
    bundle_dir = Path(manifest["bundle_dir"])
    assert bundle_dir.is_dir()
    manifest_path = bundle_dir / "manifest.json"
    assert manifest_path.exists()
    saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved_manifest["write_endpoints_called"] == []
    assert saved_manifest["expected"]["channel_client"] is False
    for item in manifest["files"]:
        evidence_path = Path(item["path"])
        assert evidence_path.exists()
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert payload["read_only"] is True
        assert payload["evidence"]["write_endpoints_called"] == []
        assert all(
            endpoint["method"] == "GET"
            for endpoint in payload["evidence"].get("endpoints", [])
        )


def test_live_acceptance_bundle_surfaces_failed_expectations(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/live_acceptance_bundle.py",
                "--server",
                server.url,
                "--output-dir",
                str(tmp_path / "evidence"),
                "--json",
                "--expect-alert-client",
                "--expect-alert-mode",
                "events",
                "--expect-alert-popup",
                "--expect-alert-overlay",
                "--check-alert-detail",
                "--expect-channel-client",
                "--expect-detector",
                "--expect-monitoring",
                "--min-targets",
                "2",
                "--min-active-ocr-targets",
                "2",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.stop()

    assert result.returncode == 1
    manifest = json.loads(result.stdout)
    assert manifest["ok"] is False
    files = {item["name"]: item for item in manifest["files"]}
    assert files["baseline"]["ok"] is True
    assert files["detector-channel"]["ok"] is False
    assert files["alert-client"]["ok"] is False
    detector_checks = {
        item["name"]: item
        for item in manifest["summary"]["detector-channel"]["checks"]
    }
    alert_checks = {
        item["name"]: item
        for item in manifest["summary"]["alert-client"]["checks"]
    }
    assert detector_checks["detector_online"]["ok"] is False
    assert detector_checks["channel_client_online"]["ok"] is False
    assert detector_checks["detector_targets"]["detail"] == "highest target_count=0 expected>=2"
    assert detector_checks["active_ocr_targets"]["detail"] == "0 active OCR target(s) expected>=2"
    assert manifest["expected"]["min_active_ocr_targets"] == 2
    assert alert_checks["alert_client_online"]["ok"] is False
    assert alert_checks["alert_client_mode"]["detail"] == "modes=[] expected=events"
    assert alert_checks["alert_client_popup"]["detail"] == "popup=False"
    assert alert_checks["alert_client_overlay"]["detail"] == "overlay=False"
    assert alert_checks["alert_detail"]["detail"] == "skipped:no recent alerts"
    assert manifest["expected"]["alert_mode"] == "events"
    assert manifest["expected"]["alert_popup"] is True
    assert manifest["expected"]["alert_overlay"] is True
    assert manifest["expected"]["alert_detail"] is True


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


def test_alert_client_module_help_runs_without_network():
    result = subprocess.run(
        [sys.executable, "-m", "app.alert_client", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "usage: alert_client.py" in result.stdout
    assert "--server" in result.stdout
    assert "--heartbeat-interval" in result.stdout
    assert "--reconnect-max-delay" in result.stdout
    assert "--hidden" in result.stdout
    assert "--ack" not in result.stdout
    assert "--poll" not in result.stdout


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
    assert "--heartbeat-interval" in payload["args"]
    assert "--reconnect-max-delay" in payload["args"]
    assert "--ack" not in payload["args"]
    assert "--poll" not in payload["args"]
    assert "--state" in payload["args"]
    state = payload["args"][payload["args"].index("--state") + 1]
    assert "EVE Sentry" in state
    assert payload["background"] is False
    assert payload["stdout"].endswith("alert-client.out.log")
    assert payload["stderr"].endswith("alert-client.err.log")


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
            "-Timeout",
            "8",
            "-HeartbeatInterval",
            "15",
            "-ReconnectMaxDelay",
            "20",
            "-Hidden",
            "-Python",
            "custom-python.exe",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    args = payload["args"]
    assert payload["python"] == "custom-python.exe"
    assert args[args.index("--server") + 1] == "http://example.invalid"
    assert args[args.index("--state") + 1] == "custom_state.json"
    assert args[args.index("--timeout") + 1] == "8"
    assert args[args.index("--heartbeat-interval") + 1] == "15"
    assert args[args.index("--reconnect-max-delay") + 1] == "20"
    assert "--hidden" in args
    assert "--ack" not in args
    assert "--poll" not in args
    assert "--details" not in args


def test_start_alert_client_powershell_script_prints_background_command():
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
            "-Background",
            "-Hidden",
            "-State",
            "C:\\EVE Sentry\\alert client state.json",
            "-LogDir",
            "C:\\EVE Sentry\\logs",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    args = payload["args"]
    decoded = base64.b64decode(payload["encoded_command"]).decode("utf-16le")
    assert payload["background"] is True
    assert payload["log_dir"] == "C:\\EVE Sentry\\logs"
    assert payload["stdout"] == "C:\\EVE Sentry\\logs\\alert-client.out.log"
    assert payload["stderr"] == "C:\\EVE Sentry\\logs\\alert-client.err.log"
    assert "--hidden" in args
    assert args[args.index("--state") + 1] == "C:\\EVE Sentry\\alert client state.json"
    assert "Set-Location -LiteralPath" in decoded
    assert "'C:\\EVE Sentry\\alert client state.json'" in decoded


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
    assert "EVE_SENTRY_INTEL_URL" not in payload["env"]
    assert payload["env"]["EVE_SENTRY_HEARTBEAT_INTERVAL"] == "15"
    assert payload["env"]["EVE_SENTRY_INTEL_TIMEOUT"] == "10"


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
            "-ChatlogDir",
            "C:\\EVE\\Chatlogs",
            "-System",
            "Tama",
            "-OcrDevice",
            "cpu",
            "-HeartbeatInterval",
            "20",
            "-Timeout",
            "4.5",
            "-NoPublish",
            "-AutoStart",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    env = payload["env"]
    assert env["EVE_SENTRY_INTEL_URL"] == "http://example.invalid"
    assert env["EVE_SENTRY_CHATLOG_DIR"] == "C:\\EVE\\Chatlogs"
    assert env["EVE_SENTRY_SYSTEM"] == "Tama"
    assert env["EVE_SENTRY_OCR_DEVICE"] == "cpu"
    assert env["EVE_SENTRY_HEARTBEAT_INTERVAL"] == "20"
    assert env["EVE_SENTRY_INTEL_TIMEOUT"] == "4.5"
    assert env["EVE_SENTRY_PUBLISH_INTEL"] == "0"
    assert env["EVE_SENTRY_AUTO_START_MONITOR"] == "1"
    assert payload["background"] is False
    assert payload["stdout"].endswith("monitor-client.out.log")
    assert payload["stderr"].endswith("monitor-client.err.log")


def test_start_monitor_client_powershell_script_prints_background_command():
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
            "-Background",
            "-Server",
            "http://example.invalid",
            "-ChatlogDir",
            "C:\\Users\\Test User\\Documents\\EVE\\logs\\Chatlogs",
            "-LogDir",
            "C:\\EVE Sentry\\logs",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    decoded = base64.b64decode(payload["encoded_command"]).decode("utf-16le")
    env = payload["env"]
    assert payload["background"] is True
    assert payload["log_dir"] == "C:\\EVE Sentry\\logs"
    assert payload["stdout"] == "C:\\EVE Sentry\\logs\\monitor-client.out.log"
    assert payload["stderr"] == "C:\\EVE Sentry\\logs\\monitor-client.err.log"
    assert env["EVE_SENTRY_INTEL_URL"] == "http://example.invalid"
    assert env["EVE_SENTRY_CHATLOG_DIR"] == "C:\\Users\\Test User\\Documents\\EVE\\logs\\Chatlogs"
    assert "'C:\\Users\\Test User\\Documents\\EVE\\logs\\Chatlogs'" in decoded


def test_run_server_builds_argv_from_environment():
    module = _load_script_module("run_server", "scripts/run_server.py")

    argv = module.build_server_argv(
        {
            "EVE_SENTRY_SERVER_HOST": "0.0.0.0",
            "EVE_SENTRY_SERVER_PORT": "9000",
            "EVE_SENTRY_SERVER_STORAGE": "postgres",
            "EVE_SENTRY_SERVER_POSTGRES_DSN": (
                "postgresql://eve:secret@db.internal:5432/eve_sentry"
            ),
            "EVE_SENTRY_SERVER_AUTH_ESI_CLIENT_ID": "ignored-member-client-id",
            "EVE_SENTRY_SERVER_AUTH_ESI_REDIRECT_URI": (
                "http://ignored.test/callback"
            ),
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
        }
    )

    assert argv == [
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
        "--storage",
        "postgres",
        "--postgres-dsn",
        "postgresql://eve:secret@db.internal:5432/eve_sentry",
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
    ]


def test_run_server_maps_report_retention_environment_option():
    module = _load_script_module("run_server_retention", "scripts/run_server.py")

    argv = module.build_server_argv(
        {"EVE_SENTRY_SERVER_REPORT_RETENTION_DAYS": "90"}
    )

    assert argv == ["--report-retention-days", "90"]


def test_run_server_maps_key_risk_control_environment_option():
    module = _load_script_module("run_server_key_risk", "scripts/run_server.py")

    argv = module.build_server_argv(
        {"EVE_SENTRY_SERVER_KEY_RISK_CONTROL": "off"}
    )

    assert argv == ["--key-risk-control", "off"]


def test_run_server_maps_inactive_intel_retention_environment_option():
    module = _load_script_module(
        "run_server_inactive_intel_retention",
        "scripts/run_server.py",
    )

    argv = module.build_server_argv(
        {"EVE_SENTRY_SERVER_INACTIVE_INTEL_RETENTION_DAYS": "30"}
    )

    assert argv == ["--inactive-intel-retention-days", "30"]


def test_run_server_maps_zkillboard_environment_options():
    module = _load_script_module("run_server_zkill", "scripts/run_server.py")

    enabled = module.build_server_argv(
        {"EVE_SENTRY_SERVER_ENABLE_ZKILL": "1"}
    )
    disabled = module.build_server_argv(
        {"EVE_SENTRY_SERVER_DISABLE_ZKILL": "true"}
    )

    assert enabled == ["--enable-killboard"]
    assert disabled == ["--disable-killboard"]


def test_run_server_maps_remote_esi_environment_options():
    module = _load_script_module("run_server_remote_esi", "scripts/run_server.py")

    argv = module.build_server_argv(
        {
            "EVE_SENTRY_SERVER_ESI_BACKEND": "remote",
            "EVE_SENTRY_SERVER_ESI_GATEWAY_URL": "http://10.233.53.17:8787",
            "EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN": "x" * 32,
            "EVE_SENTRY_SERVER_ESI_REMOTE_TIMEOUT": "8",
            "EVE_SENTRY_SERVER_ESI_NO_LOCAL_FALLBACK": "1",
        }
    )

    assert argv == [
        "--esi-backend",
        "remote",
        "--esi-gateway-url",
        "http://10.233.53.17:8787",
        "--esi-gateway-token",
        "x" * 32,
        "--esi-remote-timeout",
        "8",
        "--esi-no-local-fallback",
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

    assert module.main(["--enable-esi"]) == 7
    assert recorded == [["--host", "127.0.0.1", "--port", "8765", "--enable-esi"]]


def test_channel_smoke_posts_sample_chatlog_to_local_server(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    log_path = log_dir / "Alliance Intel_20260630_120000.txt"
    log_path.write_text(
        "\n".join(
            [
                "Listener: Alliance Intel",
                f"[ {eve_chat_timestamp()} ] Scout A > Tama +3 reds",
                f"[ {eve_chat_timestamp(1)} ] Scout B > Some Pilot in Oijanen",
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


def _load_script_module(name: str, relative_path: str):
    path = Path(relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
