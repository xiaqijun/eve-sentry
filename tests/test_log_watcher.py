import os

from app.channels.log_watcher import (
    ChatLogWatcher,
    OffsetStore,
    channel_name_from_path,
    detect_encoding,
)


def test_channel_name_strips_timestamp_suffix(tmp_path):
    path = tmp_path / "Alliance Intel_20260630_120000.txt"

    assert channel_name_from_path(path) == "Alliance Intel"


def test_channel_name_strips_eve_character_id_suffix(tmp_path):
    path = tmp_path / "wc.Venal+Br+Te_20260702_121156_2124219939.txt"

    assert channel_name_from_path(path) == "wc.Venal+Br+Te"


def test_detect_encoding_for_utf8_and_utf16():
    assert detect_encoding("hello".encode("utf-8")) == "utf-8-sig"
    assert detect_encoding("hello".encode("utf-16")) == "utf-16"


def test_offset_store_saves_with_atomic_replace(tmp_path):
    chatlog = tmp_path / "Alliance Intel_20260630_120000.txt"
    chatlog.write_text("line\n", encoding="utf-8")
    state = tmp_path / "offsets.json"
    store = OffsetStore(state)

    store.set(chatlog, 5)
    store.save()

    assert state.exists()
    assert not (tmp_path / ".offsets.json.tmp").exists()
    assert OffsetStore(state).get(chatlog) == 5


def test_offset_store_backs_up_invalid_json(tmp_path):
    state = tmp_path / "offsets.json"
    state.write_text("{broken", encoding="utf-8")

    store = OffsetStore(state)

    assert store._offsets == {}
    assert not state.exists()
    assert (tmp_path / "offsets.json.invalid").read_text(encoding="utf-8") == "{broken"


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

    watcher = ChatLogWatcher(log_dir, channels=["Alliance Intel"], state_path=state)
    first = watcher.poll_lines()

    assert [line.channel for line in first] == ["Alliance Intel"]
    assert first[0].text.endswith("Tama +3 reds")
    watcher.commit_line(first[0])

    restarted = ChatLogWatcher(log_dir, channels=["Alliance Intel"], state_path=state)

    assert restarted.poll_lines() == []

    with intel_log.open("a", encoding="utf-8") as handle:
        handle.write("[ 2026.06.30 12:02:00 ] Scout B > Oijanen Some Pilot\n")

    second = restarted.poll_lines()

    assert len(second) == 1
    assert second[0].text.endswith("Oijanen Some Pilot")


def test_watcher_reads_only_latest_file_per_channel(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    state = tmp_path / "offsets.json"
    old_log = log_dir / "Alliance Intel_20260630_120000.txt"
    latest_log = log_dir / "Alliance Intel_20260630_130000.txt"
    old_log.write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Old system +3 reds\n",
        encoding="utf-8",
    )
    latest_log.write_text(
        "[ 2026.06.30 13:01:12 ] Scout B > Latest system +1 red\n",
        encoding="utf-8",
    )
    os.utime(old_log, (1, 1))
    os.utime(latest_log, (2, 2))

    watcher = ChatLogWatcher(log_dir, channels=["Alliance Intel"], state_path=state)
    lines = watcher.poll_lines()

    assert [line.path for line in lines] == [latest_log]
    assert lines[0].text.endswith("Latest system +1 red")


def test_watcher_starts_new_files_at_end_when_requested(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    state = tmp_path / "offsets.json"
    path = log_dir / "Alliance Intel_20260630_120000.txt"
    path.write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Existing line\n",
        encoding="utf-8",
    )
    watcher = ChatLogWatcher(
        log_dir,
        channels=["Alliance Intel"],
        state_path=state,
        start_at_end_for_new_files=True,
    )

    assert watcher.poll_lines() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("[ 2026.06.30 12:02:00 ] Scout B > New line\n")

    lines = watcher.poll_lines()

    assert len(lines) == 1
    assert lines[0].text.endswith("New line")


def test_watcher_channel_filters_are_exact_by_default(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    state = tmp_path / "offsets.json"
    (log_dir / "Alliance Intel_20260630_120000.txt").write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )

    watcher = ChatLogWatcher(log_dir, channels=["Alliance"], state_path=state)

    assert watcher.poll_lines() == []


def test_watcher_channel_filters_support_explicit_wildcards(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    state = tmp_path / "offsets.json"
    (log_dir / "Alliance Intel_20260630_120000.txt").write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds\n",
        encoding="utf-8",
    )

    watcher = ChatLogWatcher(log_dir, channels=["Alliance*"], state_path=state)
    lines = watcher.poll_lines()

    assert [line.channel for line in lines] == ["Alliance Intel"]


def test_watcher_keeps_incomplete_trailing_line_until_newline(tmp_path):
    log_dir = tmp_path / "Chatlogs"
    log_dir.mkdir()
    state = tmp_path / "offsets.json"
    path = log_dir / "Alliance Intel_20260630_120000.txt"
    path.write_text(
        "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
        encoding="utf-8",
    )

    watcher = ChatLogWatcher(log_dir, channels=["Alliance Intel"], state_path=state)

    assert watcher.poll_lines() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")

    lines = watcher.poll_lines()

    assert len(lines) == 1
    assert lines[0].text.endswith("Tama +3 reds")


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
