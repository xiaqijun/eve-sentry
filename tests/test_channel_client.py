import io
import json

from app.channel_client import format_observation, parse_args, process_once
from app.channels.log_watcher import ChatLogWatcher


class FakeApi:
    def __init__(self):
        self.observations = []

    def post_observation(self, **payload):
        self.observations.append(payload)
        return {"ok": True}


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
    args = parse_args(["--once", "--dry-run", "--json", "--include-existing"])

    assert args.once is True
    assert args.dry_run is True
    assert args.json is True
    assert args.ignore_existing is False
