import re


class RosterParseError(ValueError):
    pass


COMMAND_RE = re.compile(r"^分析(?:\s+|$)", re.IGNORECASE)
HELP_RE = re.compile(r"^帮助\s*$", re.IGNORECASE)
SEPARATOR_RE = re.compile(r"[\n,，;；]+")


def is_help_command(content: str) -> bool:
    return bool(HELP_RE.fullmatch(normalize_command_content(content)))


def is_analysis_command(content: str) -> bool:
    return bool(re.fullmatch(r"分析", normalize_command_content(content), re.IGNORECASE))


def parse_roster(content: str, max_characters: int = 30) -> list[str]:
    cleaned = normalize_command_content(content)
    match = COMMAND_RE.match(cleaned)
    if not match:
        raise RosterParseError("请使用“@机器人 分析”后跟角色名单。")

    payload = cleaned[match.end() :].strip()
    if not payload:
        raise RosterParseError("角色名单不能为空。")

    names: list[str] = []
    seen: set[str] = set()
    for raw in SEPARATOR_RE.split(payload):
        name = re.sub(r"\s+", " ", raw).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)

    if not names:
        raise RosterParseError("没有识别到有效角色名。")
    if len(names) > max_characters:
        raise RosterParseError(f"一次最多分析 {max_characters} 个角色，当前为 {len(names)} 个。")
    return names


def _clean_mention_prefix(content: str) -> str:
    # QQ group events usually remove the @ token from content, while tests and
    # alternative adapters may leave it in place.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Some QQ adapters concatenate the mention and slash command without a
    # space (for example, ``@哨兵/查询人员``). Do not consume the command as
    # part of the mention token in that form.
    return re.sub(r"^\s*(?:<@!?[^>]+>|@[^\s/]+)\s*", "", content, count=1)


def normalize_command_content(content: str) -> str:
    """Normalize a QQ command by removing the bot mention and slash prefix."""
    normalized = _clean_mention_prefix(str(content or "")).strip()
    return re.sub(r"^/\s*", "", normalized, count=1).strip()
