import io
import json

from app.channel_client import parse_args, process_once, run_channel_client
from app.channels.log_watcher import ChatLogWatcher
from app.intel_client import IntelApiError


class FakeRawApi:
    def __init__(self):
        self.lines = []

    def post_channel_line(self, line, channel="", defer_enrichment=False):
        self.lines.append((line, channel, defer_enrichment))
        if line.startswith("Listener:"):
            return {"ok": True, "ignored": True}
        return {"ok": True, "ignored": False}


class FailingOnceRawApi:
    def __init__(self):
        self.calls = 0
        self.lines = []

    def post_channel_line(self, line, channel="", defer_enrichment=False):
        self.calls += 1
        if self.calls == 1:
            raise IntelApiError("server offline")
        self.lines.append((line, channel, defer_enrichment))
        return {"ok": True, "ignored": False}


def test_process_once_posts_raw_channel_lines_and_respects_offsets(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    (log_dir / "Alliance Intel_20260630_120000.txt").write_text(
        "\n".join(
            [
                "Listener: ignored header",
                "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
                "[ 2026.06.30 12:02:00 ] Scout B > Oijanen Some Pilot",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watcher = ChatLogWatcher(
        log_dir,
        channels=["Alliance Intel"],
        state_path=tmp_path / "s.json",
    )
    api = FakeRawApi()

    assert process_once(watcher, api) == 2
    assert process_once(watcher, api) == 0
    assert [item[1] for item in api.lines] == ["Alliance Intel"] * 3
    assert [item[2] for item in api.lines] == [True] * 3
    assert api.lines[0][0] == "Listener: ignored header"
    assert api.lines[1][0].endswith("Tama +3 reds")


def test_process_once_retries_raw_line_when_post_fails(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    (log_dir / "Alliance Intel_20260630_120000.txt").write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "s.json"
    watcher = ChatLogWatcher(
        log_dir,
        channels=["Alliance Intel"],
        state_path=state_path,
    )
    api = FailingOnceRawApi()

    assert process_once(watcher, api) == 0

    restarted = ChatLogWatcher(
        log_dir,
        channels=["Alliance Intel"],
        state_path=state_path,
    )

    assert process_once(restarted, api) == 1
    assert api.lines[0][1] == "Alliance Intel"
    assert api.lines[0][2] is True
    assert api.lines[0][0].endswith("Tama +3 reds")


def test_process_once_dry_run_prints_raw_json_without_api(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    (log_dir / "Alliance Intel_20260630_120000.txt").write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )
    watcher = ChatLogWatcher(log_dir, state_path=tmp_path / "s.json")
    stream = io.StringIO()

    assert process_once(
        watcher,
        api=None,
        dry_run=True,
        json_lines=True,
        stream=stream,
    ) == 1

    payload = json.loads(stream.getvalue())
    assert payload == {
        "channel": "Alliance Intel",
        "text": "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
    }


def test_process_once_requires_api_without_dry_run(tmp_path):
    watcher = ChatLogWatcher(tmp_path, state_path=tmp_path / "s.json")

    try:
        process_once(watcher, api=None)
    except ValueError as exc:
        assert "api is required" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_process_once_idle_clears_previous_diagnostics_error(tmp_path):
    watcher = ChatLogWatcher(tmp_path, state_path=tmp_path / "s.json")
    diagnostics = {"last_error": "HTTP 502"}

    assert process_once(watcher, FakeRawApi(), diagnostics=diagnostics) == 0
    assert diagnostics["last_action"] == "server_parse_idle"
    assert diagnostics["last_error"] == ""


def test_parse_args_supports_dry_run_json_mode():
    args = parse_args(
        [
            "--once",
            "--dry-run",
            "--json",
            "--include-existing",
            "--all-channels",
        ]
    )

    assert args.once is True
    assert args.dry_run is True
    assert args.json is True
    assert args.ignore_existing is False
    assert args.all_channels is True
    assert args.server == ""


def test_parse_args_requires_server_outside_dry_run():
    try:
        parse_args(["--once"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected --server to be required outside dry-run")


def test_parse_args_rejects_removed_client_parse_option():
    try:
        parse_args(["--client-parse"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected --client-parse to be rejected")


def test_run_channel_client_once_posts_raw_line_and_heartbeat(monkeypatch, tmp_path):
    class HeartbeatApi:
        instances = []

        def __init__(self, base_url, timeout=3.0):
            self.base_url = base_url
            self.timeout = timeout
            self.heartbeats = []
            self.channel_lines = []
            self.instances.append(self)

        def post_heartbeat(self, **payload):
            self.heartbeats.append(payload)
            return {"client_id": payload["client_id"], "online": True}

        def post_channel_line(self, line, channel="", defer_enrichment=False):
            self.channel_lines.append(
                {
                    "line": line,
                    "channel": channel,
                    "defer_enrichment": defer_enrichment,
                }
            )
            return {"ok": True, "ignored": False}

    monkeypatch.setattr("app.channel_client.IntelApiClient", HeartbeatApi)
    monkeypatch.setenv("EVE_SENTRY_CLIENT_VERSION", "test-version")
    monkeypatch.setenv("EVE_SENTRY_CLIENT_HOST", "test-host")
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    (log_dir / "Alliance Intel_20260630_120000.txt").write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )
    args = parse_args(
        [
            "--server",
            "http://example.invalid",
            "--log-dir",
            str(log_dir),
            "--state",
            str(tmp_path / "channel_offsets.json"),
            "--channel",
            "Alliance Intel",
            "--once",
            "--include-existing",
        ]
    )

    assert run_channel_client(args) == 0
    api = HeartbeatApi.instances[0]
    assert len(api.heartbeats) == 1
    assert api.heartbeats[0]["client_type"] == "channel_client"
    assert api.heartbeats[0]["details"]["mode"] == "server_parse"
    assert api.heartbeats[0]["details"]["last_action"] == "server_parse:1"
    assert api.heartbeats[0]["details"]["client_version"] == "test-version"
    assert api.heartbeats[0]["details"]["host"] == "test-host"
    assert api.heartbeats[0]["details"]["last_success_at"]
    assert api.heartbeats[0]["details"]["server_parse"] is True
    assert api.channel_lines == [
        {
            "line": "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
            "channel": "Alliance Intel",
            "defer_enrichment": True,
        }
    ]


def test_run_channel_client_without_channel_does_not_scan_or_post(monkeypatch, tmp_path):
    class HeartbeatApi:
        instances = []

        def __init__(self, base_url, timeout=3.0):
            self.base_url = base_url
            self.timeout = timeout
            self.heartbeats = []
            self.instances.append(self)

        def post_heartbeat(self, **payload):
            self.heartbeats.append(payload)
            return {"client_id": payload["client_id"], "online": True}

    def fail_process_once(*args, **kwargs):
        _ = args, kwargs
        raise AssertionError("unselected channel must not scan chatlogs")

    monkeypatch.setattr("app.channel_client.IntelApiClient", HeartbeatApi)
    monkeypatch.setattr("app.channel_client.process_once", fail_process_once)
    args = parse_args(
        [
            "--server",
            "http://example.invalid",
            "--log-dir",
            str(tmp_path),
            "--state",
            str(tmp_path / "channel_offsets.json"),
            "--once",
        ]
    )

    assert run_channel_client(args) == 0
    api = HeartbeatApi.instances[0]
    assert len(api.heartbeats) == 1
    assert api.heartbeats[0]["details"]["last_action"] == "channel_unselected"
