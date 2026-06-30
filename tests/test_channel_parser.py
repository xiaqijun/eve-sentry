from app.channels.parser import parse_chat_line


def parse_message(message: str):
    return parse_chat_line(
        f"[ 2026.06.30 12:01:12 ] Scout A > {message}",
        channel="Alliance Intel",
    )


def test_parse_plus_count_reds():
    parsed = parse_message("Tama +3 reds")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 3
    assert parsed.names == []
    assert parsed.confidence == 0.7
    assert parsed.seen_at == "2026-06-30T12:01:12+00:00"


def test_parse_mojibake_chinese_red_keyword():
    parsed = parse_message("Tama \u93c8\u590c\u5b69")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 1
    assert parsed.names == []


def test_parse_unicode_chinese_red_keyword():
    parsed = parse_message("Tama \u6709\u7ea2")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 1
    assert parsed.names == []


def test_parse_system_followed_by_pilot_name():
    parsed = parse_message("Oijanen Some Pilot")

    assert parsed is not None
    assert parsed.system_name == "Oijanen"
    assert parsed.hostile_count is None
    assert parsed.names == ["Some Pilot"]
    assert parsed.confidence == 0.8


def test_parse_pilot_name_before_system_location():
    parsed = parse_message("Some Pilot in Oijanen")

    assert parsed is not None
    assert parsed.system_name == "Oijanen"
    assert parsed.names == ["Some Pilot"]


def test_parse_nullsec_style_system_reds():
    parsed = parse_message("ABC-123 reds")

    assert parsed is not None
    assert parsed.system_name == "ABC-123"
    assert parsed.hostile_count == 1


def test_parse_jump_count_and_direction_metadata():
    parsed = parse_message("Tama +2 reds 3j towards Oijanen")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 2
    assert parsed.jump_count == 3
    assert parsed.direction == "Oijanen"
    assert parsed.names == []


def test_parse_hostile_report_with_location_prefix():
    parsed = parse_message("reds in Tama")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 1
    assert parsed.names == []


def test_leading_system_wins_when_location_word_points_to_noise():
    parsed = parse_message("Tama on gate")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.names == []


def test_unparsed_chat_line_keeps_raw_observation():
    parsed = parse_message("no useful structure here")

    assert parsed is not None
    assert parsed.system_name == "Unknown"
    assert parsed.raw_text == "no useful structure here"


def test_non_chat_header_line_is_ignored():
    assert parse_chat_line("Listener: Alliance Intel") is None


def test_observation_payload_keeps_channel_sender_and_raw_text():
    parsed = parse_message("Tama +3 reds")

    payload = parsed.to_observation_payload()

    assert payload["source"] == "intel_channel"
    assert payload["source_instance"] == "Alliance Intel"
    assert payload["system_name"] == "Tama"
    assert payload["raw_text"] == "Scout A: Tama +3 reds"
    assert payload["hostile_count"] == 3
    assert payload["metadata"] == {
        "sender": "Scout A",
        "channel": "Alliance Intel",
        "hostile_count": 3,
    }
