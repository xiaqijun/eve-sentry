import os
import time

from app.channels.game_logs import (
    GameConnectionLogWatcher,
    connection_state_from_line,
    game_log_id,
    trailing_game_log_id,
)


def test_connection_state_supports_chinese_and_english_messages():
    assert connection_state_from_line("与服务器的连接已被关闭。") == "offline"
    assert connection_state_from_line("The connection to the server has been closed") == "offline"
    assert connection_state_from_line("Connected to the server") == "online"
    assert connection_state_from_line("unrelated UI message") is None


def test_latest_log_is_read_incrementally_and_duplicate_lines_are_skipped(tmp_path):
    logs = tmp_path / "Gamelogs"
    logs.mkdir()
    old = logs / "20200101_000000_1001.txt"
    latest = logs / "20260826_120000_1001.txt"
    old.write_text("与服务器的连接已被关闭\n", encoding="utf-8")
    latest.write_text("normal startup\n", encoding="utf-8")
    now = time.time()
    os.utime(old, (now - 86400 * 3, now - 86400 * 3))
    os.utime(latest, (now, now))

    watcher = GameConnectionLogWatcher(logs)
    target = {"key": "window-a", "client_id": "client-a", "character_id": 1001}
    assert watcher.poll([target]) == []

    with latest.open("a", encoding="utf-8") as stream:
        stream.write("[2026.08.26 12:00:03] The connection to the server has been closed\n")
    events = watcher.poll([target])
    assert len(events) == 1
    assert events[0].state == "offline"
    assert events[0].log_id == "20260826_120000_1001"
    assert watcher.poll([target]) == []


def test_log_id_matches_separate_clients(tmp_path):
    logs = tmp_path / "Gamelogs"
    logs.mkdir()
    first = logs / "20260826_120000_1001.txt"
    second = logs / "20260826_120001_2002.txt"
    first.write_text("与服务器的连接已被关闭\n", encoding="utf-8")
    second.write_text("与服务器的连接已被关闭\n", encoding="utf-8")
    watcher = GameConnectionLogWatcher(logs)

    events = watcher.poll([
        {"key": "a", "client_id": "a", "character_id": 1001},
        {"key": "b", "client_id": "b", "character_id": 2002},
    ])

    assert {event.target_key: event.log_id for event in events} == {
        "a": "20260826_120000_1001",
        "b": "20260826_120001_2002",
    }


def test_newer_log_instance_replaces_previous_id_assignment(tmp_path):
    logs = tmp_path / "Gamelogs"
    logs.mkdir()
    first = logs / "20260826_120000_1001.txt"
    first.write_text("normal\n", encoding="utf-8")
    watcher = GameConnectionLogWatcher(logs)
    target = {"key": "a", "client_id": "a", "character_id": 1001}
    assert watcher.poll([target]) == []

    second = logs / "20260826_120001_1001.txt"
    second.write_text("与服务器的连接已被关闭\n", encoding="utf-8")
    events = watcher.poll([target])
    assert len(events) == 1
    assert events[0].log_id == second.stem


def test_log_id_helpers():
    assert game_log_id("20260826_120000_1001.txt") == "20260826_120000_1001"
    assert trailing_game_log_id("20260826_120000_1001.txt") == "1001"
    assert trailing_game_log_id("20260826_120000.txt") == "120000"
