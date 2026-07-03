from app.channels.log_watcher import ChatLogWatcher, channel_name_from_path, detect_encoding


def test_channel_name_strips_timestamp_suffix(tmp_path):
    path = tmp_path / "Alliance Intel_20260630_120000.txt"

    assert channel_name_from_path(path) == "Alliance Intel"


def test_channel_name_strips_eve_character_id_suffix(tmp_path):
    path = tmp_path / "wc.Venal+Br+Te_20260702_121156_2124219939.txt"

    assert channel_name_from_path(path) == "wc.Venal+Br+Te"


def test_detect_encoding_for_utf8_and_utf16():
    assert detect_encoding("hello".encode("utf-8")) == "utf-8-sig"
    assert detect_encoding("hello".encode("utf-16")) == "utf-16"


def test_watcher_reads_matching_files_and_persists_offsets(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    state = tmp_path / "offsets.json"
    intel_log = log_dir / "Alliance Intel_20260630_120000.txt"
    other_log = log_dir / "Corp_20260630_120000.txt"
    intel_log.write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )
    other_log.write_text(
        "[ 2026.06.30 12:01:12 ] Corp A > ignored\n",
        encoding="utf-8",
    )

    watcher = ChatLogWatcher(log_dir, channels=["Alliance"], state_path=state)
    first = watcher.poll_lines()
    restarted = ChatLogWatcher(log_dir, channels=["Alliance"], state_path=state)

    assert [line.channel for line in first] == ["Alliance Intel"]
    assert first[0].text.endswith("Tama +3 reds")
    assert restarted.poll_lines() == []

    with intel_log.open("a", encoding="utf-8") as handle:
        handle.write("[ 2026.06.30 12:02:00 ] Scout B > Oijanen Some Pilot\n")

    second = restarted.poll_lines()

    assert len(second) == 1
    assert second[0].text.endswith("Oijanen Some Pilot")


def test_watcher_reads_utf16_chatlog(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    path = log_dir / "Alliance Intel_20260630_120000.txt"
    path.write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama 有红\n",
        encoding="utf-16",
    )

    watcher = ChatLogWatcher(log_dir, state_path=tmp_path / "offsets.json")

    lines = watcher.poll_lines()

    assert len(lines) == 1
    assert lines[0].text.endswith("Tama 有红")
