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
    assert parsed.parse_pattern == "leading_system"
    assert parsed.system_candidates == ["Tama"]
    assert parsed.name_candidates == []
    assert parsed.ignored_tokens == ["+3", "reds"]
    assert parsed.confidence == 0.7
    assert parsed.seen_at == "2026-06-30T12:01:12+00:00"


def test_parse_chat_line_ignores_utf_bom_prefix():
    parsed = parse_chat_line(
        "\ufeff[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
        channel="wc.Venal+Br+Te",
    )

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 3


def test_parse_chat_line_ignores_eve_system_messages():
    parsed = parse_chat_line(
        "\ufeff[ 2026.06.30 12:01:12 ] EVE系统 > 频道置顶信息：wc.Venal+Br+Te",
        channel="wc.Venal+Br+Te",
    )

    assert parsed is None


def test_clear_status_is_not_treated_as_a_pilot_name():
    parsed = parse_message("P-UCRP* clr")

    assert parsed is not None
    assert parsed.system_name == "P-UCRP"
    assert parsed.names == []


def test_clear_status_with_movement_tail_has_no_targets():
    parsed = parse_message("HE-V4V clr went mc60")

    assert parsed is not None
    assert parsed.system_name == "HE-V4V"
    assert parsed.names == []
    assert parsed.hostile_count is None


def test_ship_only_channel_line_uses_ship_metadata_not_names():
    parsed = parse_message("C-XNUA  Retribution  Retribution  Crucifier  Crow  Omen")

    assert parsed is not None
    assert parsed.system_name == "C-XNUA"
    assert parsed.names == []
    assert parsed.hostile_count == 5
    assert parsed.ship_types == ["Retribution", "Retribution", "Crucifier", "Crow", "Omen"]
    payload = parsed.to_observation_payload()
    assert payload["metadata"]["ship_types"] == [
        "Retribution",
        "Retribution",
        "Crucifier",
        "Crow",
        "Omen",
    ]


def test_pilot_name_with_ship_suffix_splits_ship_metadata():
    parsed = parse_message("JTAU-5  pottti sabre")

    assert parsed is not None
    assert parsed.system_name == "JTAU-5"
    assert parsed.names == ["pottti"]
    assert parsed.ship_types == ["Sabre"]


def test_no_visual_suffix_is_status_not_pilot_name():
    parsed = parse_message("AH-B84  Raz0rman nv")

    assert parsed is not None
    assert parsed.system_name == "AH-B84"
    assert parsed.names == ["Raz0rman"]
    assert parsed.intel_tags == ["nv"]


def test_ess_bank_line_is_not_treated_as_pilot_name():
    parsed = parse_message("C-XNUA  Main Bank: 413 million ISK 4min40sec ess linked")

    assert parsed is not None
    assert parsed.system_name == "C-XNUA"
    assert parsed.names == []
    assert parsed.hostile_count is None
    assert "ess" in parsed.intel_tags


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
    assert parsed.name_candidates == ["Some Pilot"]
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


def test_leading_nullsec_system_wins_over_later_location_phrase():
    parsed = parse_message(
        "M-OEE8 +6 Darky Tenshou stabber x2, osprey, dictor in E-O battlestar01"
    )

    assert parsed is not None
    assert parsed.system_name == "M-OEE8"
    assert parsed.hostile_count == 6


def test_kill_line_without_system_is_not_treated_as_system_kill():
    parsed = parse_message("Kill: Prototype X89 (Heron)")

    assert parsed is not None
    assert parsed.system_name == "Unknown"
    assert parsed.parse_pattern == "raw_unparsed"


def test_plain_chatter_is_not_treated_as_a_system():
    parsed = parse_message("catch bubble on gate to E-O and on raitaru near E-O gate")

    assert parsed is not None
    assert parsed.system_name == "Unknown"


def test_parse_jump_count_and_direction_metadata():
    parsed = parse_message("Tama +2 reds 3j towards Oijanen")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 2
    assert parsed.jump_count == 3
    assert parsed.direction == "Oijanen"
    assert parsed.names == []
    assert parsed.system_candidates == ["Tama", "Oijanen"]
    assert parsed.ignored_tokens == ["+2", "reds", "3j", "Oijanen", "towards"]


def test_parse_hostile_report_with_location_prefix():
    parsed = parse_message("reds in Tama")

    assert parsed is not None
    assert parsed.system_name == "Tama"
    assert parsed.hostile_count == 1
    assert parsed.names == []
    assert parsed.parse_pattern == "located_system"


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
    assert parsed.parse_pattern == "raw_unparsed"


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
        "parse_diagnostics": {
            "parse_pattern": "leading_system",
            "system_candidates": ["Tama"],
            "ignored_tokens": ["+3", "reds"],
        },
    }
