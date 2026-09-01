import os
import time

from app.channels.game_logs import (
    GameConnectionLogWatcher,
    connection_state_from_line,
    game_log_id,
    trailing_game_log_id,
)


def test_connection_state_matches_real_chinese_disconnect_messages() -> None:
    lines = (
        "你的计算机已与EVE Online服务器断开网络通信。",
        "无法连接到指定地址。可能你没有建立因特网连接。",
        "服务器当前不接受连接",
        "连接丢失 - 你使用的服务器进程已离线",
    )

    assert all(connection_state_from_line(line) == "offline" for line in lines)


def test_connection_state_matches_existing_messages() -> None:
    assert connection_state_from_line("与服务器的连接已被关闭。") == "offline"
    assert connection_state_from_line("Disconnected from the server") == "offline"
    assert connection_state_from_line("已连接到服务器") == "online"
    assert connection_state_from_line("Connected to the server") == "online"


def test_connection_state_ignores_unrelated_log_lines() -> None:
    assert connection_state_from_line("舰船已停靠在空间站。") is None


def test_latest_log_is_read_incrementally_and_duplicate_lines_are_skipped(
    tmp_path,
) -> None:
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
        stream.write(
            "[2026.08.26 12:00:03] "
            "你的计算机已与EVE Online服务器断开网络通信。\n"
        )
    events = watcher.poll([target])
    assert len(events) == 1
    assert events[0].state == "offline"
    assert events[0].log_id == "20260826_120000_1001"
    assert watcher.poll([target]) == []


def test_log_id_matches_separate_clients(tmp_path) -> None:
    logs = tmp_path / "Gamelogs"
    logs.mkdir()
    first = logs / "20260826_120000_1001.txt"
    second = logs / "20260826_120001_2002.txt"
    first.write_text("与服务器的连接已被关闭\n", encoding="utf-8")
    second.write_text("与服务器的连接已被关闭\n", encoding="utf-8")
    watcher = GameConnectionLogWatcher(logs)

    events = watcher.poll(
        [
            {"key": "a", "client_id": "a", "character_id": 1001},
            {"key": "b", "client_id": "b", "character_id": 2002},
        ]
    )

    assert {event.target_key: event.log_id for event in events} == {
        "a": "20260826_120000_1001",
        "b": "20260826_120001_2002",
    }


def test_newer_log_instance_replaces_previous_id_assignment(tmp_path) -> None:
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


def test_timestamp_only_log_is_shared_by_multiple_windows(tmp_path) -> None:
    logs = tmp_path / "Gamelogs"
    logs.mkdir()
    log = logs / "20260831_110656.txt"
    log.write_text("你的计算机已与EVE Online服务器断开网络通信\n", encoding="utf-8")
    watcher = GameConnectionLogWatcher(logs)

    events = watcher.poll(
        [
            {"key": "a", "client_id": "a", "character_id": 1001},
            {"key": "b", "client_id": "b", "character_id": 2002},
        ]
    )

    assert {(event.target_key, event.log_id) for event in events} == {
        ("a", log.stem),
        ("b", log.stem),
    }


def test_log_id_helpers() -> None:
    assert game_log_id("20260826_120000_1001.txt") == "20260826_120000_1001"
    assert trailing_game_log_id("20260826_120000_1001.txt") == "1001"
    assert trailing_game_log_id("20260826_120000.txt") == ""
