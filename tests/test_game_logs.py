from app.channels.game_logs import connection_state_from_line


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
