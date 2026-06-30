from app.channel_client import process_once
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
    watcher = ChatLogWatcher(log_dir, channels=["Alliance"], state_path=tmp_path / "s.json")
    api = FakeApi()

    assert process_once(watcher, api) == 2
    assert process_once(watcher, api) == 0
    assert [item["system_name"] for item in api.observations] == ["Tama", "Oijanen"]
    assert api.observations[0]["source"] == "intel_channel"
    assert api.observations[1]["names"] == ["Some Pilot"]

