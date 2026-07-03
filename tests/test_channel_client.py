import io
import json

from app.channel_client import (
    format_observation,
    parse_args,
    process_once,
    run_channel_client,
)
from app.channels.log_watcher import ChatLogWatcher


class FakeApi:
    def __init__(self):
        self.observations = []

    def post_observation(self, **payload):
        self.observations.append(payload)
        return {"ok": True}


class FakeRawApi:
    def __init__(self):
        self.lines = []

    def post_channel_line(self, line, channel=""):
        self.lines.append((line, channel))
        if line.startswith("Listener:"):
            return {"ok": True, "ignored": True}
        return {
            "ok": True,
            "ignored": False,
            "observation": {"id": str(len(self.lines))},
        }


def test_process_once_posts_parsed_observations_and_respects_offsets(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    path = log_dir / "Alliance Intel_20260630_120000.txt"
    path.write_text(
        "\n".join(
            [
                "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
                "Listener: ignored header",
                "[ 2026.06.30 12:02:00 ] Scout B > Oijanen Some Pilot",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watcher = ChatLogWatcher(
        log_dir,
        channels=["Alliance"],
        state_path=tmp_path / "s.json",
    )
    api = FakeApi()

    assert process_once(watcher, api) == 2
    assert process_once(watcher, api) == 0
    assert [item["system_name"] for item in api.observations] == ["Tama", "Oijanen"]
    assert api.observations[0]["source"] == "intel_channel"
    assert api.observations[0]["metadata"]["hostile_count"] == 3
    assert api.observations[0]["metadata"]["sender"] == "Scout A"
    assert api.observations[1]["names"] == ["Some Pilot"]


def test_process_once_can_delegate_raw_lines_to_server_parser(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    path = log_dir / "Alliance Intel_20260630_120000.txt"
    path.write_text(
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
        channels=["Alliance"],
        state_path=tmp_path / "s.json",
    )
    api = FakeRawApi()

    assert process_once(watcher, api, server_parse=True) == 2
    assert process_once(watcher, api, server_parse=True) == 0
    assert [item[1] for item in api.lines] == ["Alliance Intel"] * 3
    assert api.lines[0][0] == "Listener: ignored header"
    assert api.lines[1][0].endswith("Tama +3 reds")


def test_process_once_dry_run_prints_json_without_api(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    path = log_dir / "Alliance Intel_20260630_120000.txt"
    path.write_text(
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
    assert payload["system_name"] == "Tama"
    assert payload["metadata"]["hostile_count"] == 3
    assert payload["metadata"]["sender"] == "Scout A"


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

    assert process_once(watcher, FakeApi(), diagnostics=diagnostics) == 0
    assert diagnostics["last_action"] == "observation_idle"
    assert diagnostics["last_error"] == ""


def test_format_observation_includes_metadata_summary():
    text = format_observation(
        {
            "system_name": "Tama",
            "raw_text": "Scout A: Tama +3 reds",
            "metadata": {
                "hostile_count": 3,
                "jump_count": 2,
                "direction": "Oijanen",
            },
        }
    )

    assert text == (
        "Tama: Scout A: Tama +3 reds (3 hostiles; 2 jumps; toward Oijanen)"
    )


def test_parse_args_supports_dry_run_json_mode():
    args = parse_args(
        ["--once", "--dry-run", "--json", "--include-existing", "--server-parse"]
    )

    assert args.once is True
    assert args.dry_run is True
    assert args.json is True
    assert args.ignore_existing is False
    assert args.server_parse is True


def test_run_channel_client_once_posts_heartbeat(monkeypatch, tmp_path):
    class HeartbeatApi:
        instances = []

        def __init__(self, base_url, timeout=3.0):
            self.base_url = base_url
            self.timeout = timeout
            self.heartbeats = []
            self.observations = []
            self.instances.append(self)

        def post_heartbeat(self, **payload):
            self.heartbeats.append(payload)
            return {"client_id": payload["client_id"], "online": True}

        def post_observation(self, **payload):
            self.observations.append(payload)
            return {"ok": True}

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
            "--once",
            "--include-existing",
        ]
    )

    assert run_channel_client(args) == 0
    api = HeartbeatApi.instances[0]
    assert len(api.heartbeats) == 1
    assert api.heartbeats[0]["client_type"] == "channel_client"
    assert api.heartbeats[0]["details"]["mode"] == "observation"
    assert api.heartbeats[0]["details"]["last_action"] == "observation:1"
    assert api.heartbeats[0]["details"]["client_version"] == "test-version"
    assert api.heartbeats[0]["details"]["host"] == "test-host"
    assert api.heartbeats[0]["details"]["last_success_at"]
    assert api.heartbeats[0]["details"]["server_parse"] is False
    assert api.observations[0]["system_name"] == "Tama"
