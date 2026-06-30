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
    r"\b(in|at|near|on|gate|d-?scan|scan|from|to|towards?|toward)\b"
    r"|\u65b9\u5411",
    re.IGNORECASE,
)
SPLIT_RE = re.compile(r"[,;|/]+|\s{2,}")
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
}


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


def parse_chat_line(line: str, channel: str = "") -> ParsedIntelLine | None:
    """Parse one EVE chatlog line.

    Non-chat header lines return None. Chat messages that cannot be parsed still
    return a low-confidence raw observation so the service keeps the evidence.
    """
    match = CHAT_LINE_RE.match(line.strip())
    if not match:
        return None

    message = match.group("message").strip()
    sender = match.group("sender").strip()
    seen_at = parse_eve_timestamp(match.group("timestamp"))
    if not message:
        return _raw_line(channel, sender, message, seen_at, confidence=0.1)

    system = extract_system(message)
    if not system:
        return _raw_line(channel, sender, message, seen_at, confidence=0.2)

    rest = remove_system(message, system)
    hostile_count = extract_hostile_count(rest)
    jump_count = extract_jump_count(rest)
    direction = extract_direction(rest)
    names = extract_names(rest)
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
    )


def _raw_line(
    channel: str,
    sender: str,
    message: str,
    seen_at: str,
    confidence: float,
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
    )


def parse_eve_timestamp(value: str) -> str:
    """Parse an EVE chat timestamp as UTC ISO-8601."""
    dt = datetime.strptime(value, "%Y.%m.%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).isoformat()


def extract_system(message: str) -> str:
    """Return the best system token from a message."""
    located = LOCATED_SYSTEM_RE.search(message)
    if located:
        system = located.group("system") or located.group("system_cn") or ""
        if _is_system_candidate(system):
            return system

    match = LEADING_SYSTEM_RE.match(message.strip())
    if not match:
        return ""
    system = match.group("system")
    return system if _is_system_candidate(system) else ""


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


def extract_names(message: str) -> list[str]:
    """Extract conservative pilot-name candidates after the system token."""
    text = message.strip(" :-,;")
    if not text:
        return []

    text = DIRECTION_RE.sub(" ", text)
    text = JUMP_RE.sub(" ", text)
    text = COUNT_RE.sub(" ", text)
    text = CHINESE_COUNT_RE.sub(" ", text)
    text = HOSTILE_WORD_RE.sub(" ", text)
    text = NOISE_WORD_RE.sub(" ", text)
    text = text.strip(" :-,;")
    if not text:
        return []

    names = []
    for chunk in SPLIT_RE.split(text):
        name = " ".join(part for part in chunk.split() if part)
        if _is_name_candidate(name):
            names.append(name)
    return names


def _is_system_candidate(value: str) -> bool:
    return value.casefold() not in SYSTEM_STOP_WORDS and not value.isdigit()


def _is_name_candidate(value: str) -> bool:
    if len(value) < 3:
        return False
    folded = value.casefold()
    if folded in SYSTEM_STOP_WORDS:
        return False
    return not value.isdigit()
