import pytest

from eve_risk.parser import (
    RosterParseError,
    is_analysis_command,
    is_help_command,
    parse_roster,
)


def test_parse_multiline_names_and_preserve_internal_spaces() -> None:
    assert parse_roster("分析\nCharacter One\nCharacter Two") == [
        "Character One",
        "Character Two",
    ]


def test_parse_mixed_separators_and_deduplicate_case_insensitively() -> None:
    assert parse_roster("分析 Alice Example，Bob Example;alice example") == [
        "Alice Example",
        "Bob Example",
    ]


def test_parse_rejects_empty_and_over_limit() -> None:
    with pytest.raises(RosterParseError, match="不能为空"):
        parse_roster("分析")
    with pytest.raises(RosterParseError, match="最多"):
        parse_roster("分析 " + ",".join(f"Pilot {index}" for index in range(31)))


def test_help_command() -> None:
    assert is_help_command("帮助")
    assert is_help_command("@机器人 帮助")


def test_bare_analysis_command() -> None:
    assert is_analysis_command("分析")
    assert is_analysis_command("<@!bot> /分析")
    assert not is_analysis_command("分析 Alice")
