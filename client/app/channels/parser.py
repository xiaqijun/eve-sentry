"""Parse EVE chatlog lines into canonical intel observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SYSTEM_TOKEN = r"[A-Za-z0-9][A-Za-z0-9-]{2,15}"
CHAT_LINE_RE = re.compile(
    r"^\[\s*(?P<timestamp>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s*"
    r"(?P<sender>.*?)\s*>\s*(?P<message>.*)$"
)
LEADING_SYSTEM_RE = re.compile(rf"^(?P<system>{SYSTEM_TOKEN})\b")
SYSTEM_TOKEN_RE = re.compile(rf"\b(?P<system>{SYSTEM_TOKEN})\b")
LOCATED_SYSTEM_RE = re.compile(
    rf"\b(?:in|at|near|on)\s+(?P<system>{SYSTEM_TOKEN})\b"
    rf"|(?:\u5728|\u5230|\u53bb|\u5f80|\u5411)\s*(?P<system_cn>{SYSTEM_TOKEN})",
    re.IGNORECASE,
)
COUNT_RE = re.compile(
    r"(?:^|[\s,;])(?:[+xX](?P<count>\d+)|"
    r"(?P<count2>\d+)\s*(?:red|reds|hostile|hostiles|neut|neuts)\b)",
    re.IGNORECASE,
)
CHINESE_COUNT_RE = re.compile(
    r"(?P<count>\d+)\s*(?:\u4e2a)?"
    r"(?:\u7ea2|\u654c\u5bf9|\u4e2d\u7acb|\u93c8\u590c\u5b69)"
)
HOSTILE_WORD_RE = re.compile(
    r"\b(red|reds|hostile|hostiles|neut|neuts)\b"
    r"|\u6709\u7ea2|\u7ea2|\u654c\u5bf9|\u4e2d\u7acb|\u93c8\u590c\u5b69",
    re.IGNORECASE,
)
JUMP_RE = re.compile(
    r"\b(?P<count>\d+)\s*(?:jumps?|jp|j)\b|(?P<count_cn>\d+)\s*\u8df3",
    re.IGNORECASE,
)
DIRECTION_RE = re.compile(
    rf"\b(?:to|towards?)\s+(?P<target>{SYSTEM_TOKEN})\b"
    rf"|(?:\u5f80|\u5411|\u53bb|\u5230)\s*(?P<target_cn>{SYSTEM_TOKEN})",
    re.IGNORECASE,
)
NOISE_WORD_RE = re.compile(
    r"\b(in|at|near|on|gate|d-?scan|scan|from|to|towards?|toward|clr|clear)\b"
    r"|\u65b9\u5411",
    re.IGNORECASE,
)
SPLIT_RE = re.compile(r"[,;|/]+|\s{2,}")
SHIP_TERMS = [
    ("hurricane fleet issue", "Hurricane Fleet Issue"),
    ("stabber fleet issue", "Stabber Fleet Issue"),
    ("retribution", "Retribution"),
    ("crucifier", "Crucifier"),
    ("confessor", "Confessor"),
    ("flycatcher", "Flycatcher"),
    ("hyperion", "Hyperion"),
    ("cyclones", "Cyclone"),
    ("cyclone", "Cyclone"),
    ("buzzard", "Buzzard"),
    ("vedmak", "Vedmak"),
    ("stabber", "Stabber"),
    ("orthrus", "Orthrus"),
    ("astarte", "Astarte"),
    ("rapier", "Rapier"),
    ("kronos", "Kronos"),
    ("oracle", "Oracle"),
    ("osprey", "Osprey"),
    ("dictor", "Dictor"),
    ("hecate", "Hecate"),
    ("sabre", "Sabre"),
    ("sabers", "Sabre"),
    ("saber", "Sabre"),
    ("hound", "Hound"),
    ("helios", "Helios"),
    ("heron", "Heron"),
    ("tornado", "Tornado"),
    ("crow", "Crow"),
    ("omen", "Omen"),
]
SYSTEM_STOP_WORDS = {
    "red",
    "reds",
    "hostile",
    "hostiles",
    "neut",
    "neuts",
    "gate",
    "dscan",
    "scan",
    "in",
    "at",
    "near",
    "to",
    "from",
    "toward",
    "towards",
    "kill",
    "catch",
    "bubble",
    "ship",
    "clr",
    "clear",
}
INLINE_SENDER_STOP_WORDS = {
    "kill",
    "loss",
    "report",
    "intel",
    "status",
    "main bank",
}


@dataclass(frozen=True)
class TargetDetails:
    names: list[str]
    ship_types: list[str]
    intel_tags: list[str]


@dataclass(frozen=True)
class ParsedIntelLine:
    """A parsed intel-channel chat line."""

    channel: str
    sender: str
    system_name: str
    raw_text: str
    seen_at: str
    names: list[str]
    hostile_count: int | None
    confidence: float
    jump_count: int | None = None
    direction: str = ""
    parse_pattern: str = ""
    system_candidates: list[str] | None = None
    name_candidates: list[str] | None = None
    ignored_tokens: list[str] | None = None
    ship_types: list[str] | None = None
    intel_tags: list[str] | None = None

    def to_observation_payload(self) -> dict[str, Any]:
        """Return the API payload for POST /api/observations."""
        raw_text = self.raw_text
        if self.sender:
            raw_text = f"{self.sender}: {raw_text}"

        metadata: dict[str, Any] = {}
        if self.sender:
            metadata["sender"] = self.sender
        if self.channel:
            metadata["channel"] = self.channel
        if self.hostile_count is not None:
            metadata["hostile_count"] = self.hostile_count
        if self.jump_count is not None:
            metadata["jump_count"] = self.jump_count
        if self.direction:
            metadata["direction"] = self.direction
        if self.ship_types:
            metadata["ship_types"] = list(self.ship_types)
        if self.intel_tags:
            metadata["intel_tags"] = list(self.intel_tags)
        diagnostics = self._parse_diagnostics()
        if diagnostics:
            metadata["parse_diagnostics"] = diagnostics

        payload: dict[str, Any] = {
            "source": "intel_channel",
            "source_instance": self.channel,
            "system_name": self.system_name,
            "names": list(self.names),
            "raw_text": raw_text,
            "confidence": self.confidence,
            "seen_at": self.seen_at,
            "metadata": metadata,
        }
        if self.hostile_count is not None:
            payload["hostile_count"] = self.hostile_count
        return payload

    def _parse_diagnostics(self) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {}
        if self.parse_pattern:
            diagnostics["parse_pattern"] = self.parse_pattern
        if self.system_candidates:
            diagnostics["system_candidates"] = list(self.system_candidates)
        if self.name_candidates:
            diagnostics["name_candidates"] = list(self.name_candidates)
        if self.ignored_tokens:
            diagnostics["ignored_tokens"] = list(self.ignored_tokens)
        return diagnostics


def parse_chat_line(line: str, channel: str = "") -> ParsedIntelLine | None:
    """Parse one EVE chatlog line.

    Non-chat header lines return None. Chat messages that cannot be parsed still
    return a low-confidence raw observation so the service keeps the evidence.
    """
    normalized_line = line.strip().lstrip("\ufeff")
    match = CHAT_LINE_RE.match(normalized_line)
    if not match:
        return None

    sender = match.group("sender").strip()
    if sender.casefold().startswith("eve"):
        return None
    message = strip_inline_sender_prefix(
        strip_repeated_sender_prefix(match.group("message"), sender)
    )
    seen_at = parse_eve_timestamp(match.group("timestamp"))
    system_candidates = extract_system_candidates(message)
    if not message:
        return _raw_line(
            channel,
            sender,
            message,
            seen_at,
            confidence=0.1,
            parse_pattern="empty_message",
            system_candidates=system_candidates,
        )
    system = extract_system(message)
    if not system:
        return _raw_line(
            channel,
            sender,
            message,
            seen_at,
            confidence=0.2,
            parse_pattern="raw_unparsed",
            system_candidates=system_candidates,
            name_candidates=extract_names(message),
        )

    rest = remove_system(message, system)
    hostile_count = extract_hostile_count(rest)
    jump_count = extract_jump_count(rest)
    direction = extract_direction(rest)
    details = (
        TargetDetails(names=[], ship_types=[], intel_tags=[])
        if _is_clear_status(rest) and hostile_count is None
        else extract_target_details(rest)
    )
    if hostile_count is None and not details.names and details.ship_types:
        hostile_count = len(details.ship_types)
    names = details.names
    parse_pattern = _system_parse_pattern(message, system)
    ignored_tokens = extract_ignored_tokens(rest, direction=direction)
    confidence = 0.75
    if names:
        confidence = 0.8
    elif hostile_count is not None:
        confidence = 0.7

    return ParsedIntelLine(
        channel=channel,
        sender=sender,
        system_name=system,
        raw_text=message,
        seen_at=seen_at,
        names=names,
        hostile_count=hostile_count,
        confidence=confidence,
        jump_count=jump_count,
        direction=direction,
        parse_pattern=parse_pattern,
        system_candidates=system_candidates,
        name_candidates=names,
        ignored_tokens=ignored_tokens,
        ship_types=details.ship_types,
        intel_tags=details.intel_tags,
    )


def _raw_line(
    channel: str,
    sender: str,
    message: str,
    seen_at: str,
    confidence: float,
    parse_pattern: str = "raw_unparsed",
    system_candidates: list[str] | None = None,
    name_candidates: list[str] | None = None,
) -> ParsedIntelLine:
    return ParsedIntelLine(
        channel=channel,
        sender=sender,
        system_name="Unknown",
        raw_text=message,
        seen_at=seen_at,
        names=[],
        hostile_count=None,
        confidence=confidence,
        parse_pattern=parse_pattern,
        system_candidates=system_candidates or [],
        name_candidates=name_candidates or [],
    )


def strip_repeated_sender_prefix(message: str, sender: str) -> str:
    """Remove a repeated chat sender from the start of the message body."""
    text = str(message or "").strip()
    sender_text = str(sender or "").strip()
    if not text or not sender_text:
        return text
    if not text.casefold().startswith(sender_text.casefold()):
        return text
    rest = text[len(sender_text):]
    if rest and rest[0] not in " \t:：->-—":
        return text
    return rest.strip(" \t:：->-—,;")


def strip_inline_sender_prefix(message: str) -> str:
    """Remove a leading ``sender:`` wrapper when the body contains intel."""
    text = str(message or "").strip()
    match = re.match(r"^(?P<sender>[^:：]{3,64})[:：]\s*(?P<body>.+)$", text)
    if not match:
        return text

    sender = match.group("sender").strip()
    body = match.group("body").strip()
    if not sender or not body or not re.search(r"[A-Za-z0-9]", sender):
        return text
    if sender.casefold() in INLINE_SENDER_STOP_WORDS:
        return text
    if extract_system(body) or extract_hostile_count(body) is not None:
        return body
    return text


def parse_eve_timestamp(value: str) -> str:
    """Parse an EVE chat timestamp as UTC ISO-8601."""
    dt = datetime.strptime(value, "%Y.%m.%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).isoformat()


def extract_system(message: str) -> str:
    """Return the best system token from a message."""
    stripped = message.strip()
    leading = LEADING_SYSTEM_RE.match(stripped)
    if leading:
        leading_system = leading.group("system")
        if leading_system.casefold() == "catch":
            return ""
        if _looks_like_nullsec_system(leading_system):
            return leading_system

    located = LOCATED_SYSTEM_RE.search(message)
    if located:
        system = located.group("system") or located.group("system_cn") or ""
        if _is_system_candidate(system):
            return system

    later_nullsec = _first_nullsec_system(stripped[leading.end():] if leading else "")
    if later_nullsec:
        return later_nullsec

    if not leading:
        return ""
    system = leading.group("system")
    return system if _is_plausible_leading_system(system) else ""


def extract_system_candidates(message: str) -> list[str]:
    """Return ordered system-like tokens that may be worth resolver validation."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        token = value.strip()
        key = token.casefold()
        if not token or key in seen or not _is_system_candidate(token):
            return
        seen.add(key)
        candidates.append(token)

    located = LOCATED_SYSTEM_RE.search(message)
    if located:
        add(located.group("system") or located.group("system_cn") or "")

    leading = LEADING_SYSTEM_RE.match(message.strip())
    if leading:
        add(leading.group("system"))

    direction = DIRECTION_RE.search(message)
    if direction:
        add(direction.group("target") or direction.group("target_cn") or "")

    for match in SYSTEM_TOKEN_RE.finditer(message):
        add(match.group("system"))
    return candidates


def remove_system(message: str, system: str) -> str:
    """Remove the parsed system token and nearby location words."""
    stripped = message.strip()
    leading = LEADING_SYSTEM_RE.match(stripped)
    if leading and leading.group("system") == system:
        return stripped[leading.end():].strip(" :-,;")

    escaped = re.escape(system)
    text = re.sub(
        rf"\b(?:in|at|near|on)\s+{escaped}\b",
        " ",
        stripped,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"(?:\u5728|\u5230|\u53bb|\u5f80|\u5411)\s*{escaped}",
        " ",
        text,
        count=1,
    )
    text = re.sub(rf"\b{escaped}\b", " ", text, count=1, flags=re.IGNORECASE)
    return text.strip(" :-,;")


def extract_hostile_count(message: str) -> int | None:
    """Extract a hostile count from common intel shorthand."""
    match = COUNT_RE.search(message)
    if match:
        raw = match.group("count") or match.group("count2")
        return int(raw)
    chinese_match = CHINESE_COUNT_RE.search(message)
    if chinese_match:
        return int(chinese_match.group("count"))
    if HOSTILE_WORD_RE.search(message):
        return 1
    return None


def extract_jump_count(message: str) -> int | None:
    """Extract distance in jumps when a channel line includes it."""
    match = JUMP_RE.search(message)
    if not match:
        return None
    raw = match.group("count") or match.group("count_cn")
    return int(raw)


def extract_direction(message: str) -> str:
    """Extract a destination or movement direction token."""
    match = DIRECTION_RE.search(message)
    if not match:
        return ""
    return match.group("target") or match.group("target_cn") or ""


def _is_clear_status(message: str) -> bool:
    return bool(re.search(r"\b(?:clr|clear)\b", message, re.IGNORECASE))


def extract_ignored_tokens(message: str, direction: str = "") -> list[str]:
    """Return parser-consumed tokens that were not treated as names."""
    ignored: list[str] = []

    def add(value: str) -> None:
        token = value.strip(" :-,;")
        if not token:
            return
        key = token.casefold()
        if key not in {item.casefold() for item in ignored}:
            ignored.append(token)

    for match in COUNT_RE.finditer(message):
        add(match.group(0))
    for match in CHINESE_COUNT_RE.finditer(message):
        add(match.group(0))
    for match in HOSTILE_WORD_RE.finditer(message):
        add(match.group(0))
    for match in JUMP_RE.finditer(message):
        add(match.group(0))
    if direction:
        add(direction)

    for match in NOISE_WORD_RE.finditer(message):
        add(match.group(0))
    return ignored


def extract_names(message: str) -> list[str]:
    """Extract conservative pilot-name candidates after the system token."""
    return extract_target_details(message).names


def extract_target_details(message: str) -> TargetDetails:
    """Extract pilot names while preserving ship and status context."""
    text = message.strip(" :-,;")
    if not text:
        return TargetDetails(names=[], ship_types=[], intel_tags=[])

    text = DIRECTION_RE.sub(" ", text)
    text = JUMP_RE.sub(" ", text)
    text = COUNT_RE.sub(" ", text)
    text = CHINESE_COUNT_RE.sub(" ", text)
    text = HOSTILE_WORD_RE.sub(" ", text)
    text = NOISE_WORD_RE.sub(" ", text)
    text = text.strip(" :-,;")
    if not text:
        return TargetDetails(names=[], ship_types=[], intel_tags=[])

    names = []
    ship_types: list[str] = []
    intel_tags: list[str] = []
    for chunk in SPLIT_RE.split(text):
        name, ships, tags = _parse_target_chunk(chunk)
        ship_types.extend(ships)
        _extend_unique(intel_tags, tags)
        if _is_name_candidate(name):
            names.append(name)
    return TargetDetails(names=names, ship_types=ship_types, intel_tags=intel_tags)


def _parse_target_chunk(value: str) -> tuple[str, list[str], list[str]]:
    text = " ".join(part for part in value.split() if part).strip(" *:-,;")
    if not text:
        return "", [], []

    tags: list[str] = []
    if re.search(r"\bnv\b", text, re.IGNORECASE):
        tags.append("nv")
        text = re.sub(r"\bnv\b", " ", text, flags=re.IGNORECASE)
    if re.search(r"\bess'?s?\b|main\s+bank|million\s+isk", text, re.IGNORECASE):
        tags.append("ess")
        if re.search(r"main\s+bank|million\s+isk|linked", text, re.IGNORECASE):
            return "", [], tags
        text = re.sub(r"\bess'?s?\b", " ", text, flags=re.IGNORECASE)

    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\s*(?:min|mins|minutes?|sec|secs|seconds?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\s*x\b|\bx\s*\d+\b", " ", text, flags=re.IGNORECASE)

    ships: list[str] = []
    for term, canonical in SHIP_TERMS:
        pattern = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(term)}s?(?![A-Za-z0-9-])", re.IGNORECASE)
        while True:
            match = pattern.search(text)
            if match is None:
                break
            ships.append(canonical)
            text = f"{text[:match.start()]} {text[match.end():]}"

    text = re.sub(r"\b(?:linked|stealing|least|with|them|out|just|minused|probe|disrupt)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+(?:kk|m|b)?\b", " ", text, flags=re.IGNORECASE)
    name = " ".join(part for part in text.split() if part).strip(" *:-,;")
    if _looks_like_nullsec_system(name):
        name = ""
    return name, ships, tags


def _extend_unique(target: list[str], values: list[str]) -> None:
    seen = {item.casefold() for item in target}
    for value in values:
        key = value.casefold()
        if key not in seen:
            target.append(value)
            seen.add(key)


def _is_system_candidate(value: str) -> bool:
    return value.casefold() not in SYSTEM_STOP_WORDS and not value.isdigit()


def _is_plausible_leading_system(value: str) -> bool:
    return _is_system_candidate(value) and (
        _looks_like_nullsec_system(value) or not value.islower()
    )


def _looks_like_nullsec_system(value: str) -> bool:
    token = value.strip(" *:-,;")
    return _is_system_candidate(token) and "-" in token.strip("-")


def _first_nullsec_system(value: str) -> str:
    for match in SYSTEM_TOKEN_RE.finditer(value):
        token = match.group("system").strip(" *:-,;")
        if _looks_like_nullsec_system(token):
            return token
    return ""


def _is_name_candidate(value: str) -> bool:
    if len(value) < 3:
        return False
    folded = value.casefold()
    if folded in SYSTEM_STOP_WORDS:
        return False
    return not value.isdigit()


def _system_parse_pattern(message: str, system: str) -> str:
    located = LOCATED_SYSTEM_RE.search(message)
    if located:
        located_system = located.group("system") or located.group("system_cn") or ""
        if located_system == system:
            return "located_system"
    leading = LEADING_SYSTEM_RE.match(message.strip())
    if leading and leading.group("system") == system:
        return "leading_system"
    return "system_candidate"
