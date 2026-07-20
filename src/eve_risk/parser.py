import re


class RosterParseError(ValueError):
    pass


COMMAND_RE = re.compile(r"^(?:\s*@\S+\s*)?(?:/)?分析(?:\s+|$)", re.IGNORECASE)
HELP_RE = re.compile(r"^(?:\s*@\S+\s*)?(?:/)?帮助\s*$", re.IGNORECASE)
SEPARATOR_RE = re.compile(r"[\n,，;；]+")


def is_help_command(content: str) -> bool:
    return bool(HELP_RE.match(_clean_mention_prefix(content).strip()))


def parse_roster(content: str, max_characters: int = 30) -> list[str]:
    cleaned = _clean_mention_prefix(content).strip()
    match = re.match(r"^(?:/)?分析(?:\s+|$)", cleaned, re.IGNORECASE)
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
    return re.sub(r"^\s*<@!?\w+>\s*", "", content, count=1)
