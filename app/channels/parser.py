"""Parse EVE chatlog lines into canonical intel observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


CHAT_LINE_RE = re.compile(
    r"^\[\s*(?P<timestamp>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s*"
    r"(?P<sender>.*?)\s*>\s*(?P<message>.*)$"
)
COUNT_RE = re.compile(r"(?:^|\s)[+xX](?P<count>\d+)\b|(?P<count2>\d+)\s*(?:red|reds|hostile|hostiles)\b", re.IGNORECASE)
SYSTEM_RE = re.compile(r"^(?P<system>[A-Za-z0-9][A-Za-z0-9-]{2,15})\b")
HOSTILE_WORD_RE = re.compile(r"\b(red|reds|hostile|hostiles|neut|neuts)\b|有红|红", re.IGNORECASE)
NOISE_RE = re.compile(r"\b(in|at|near|to|from|towards?|gate|d-?scan|方向)\b", re.IGNORECASE)


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

    def to_observation_payload(self) -> dict[str, Any]:
        """Return the API payload for POST /api/observations."""
        raw_text = self.raw_text
        if self.sender:
            raw_text = f"{self.sender}: {raw_text}"
        payload: dict[str, Any] = {
            "source": "intel_channel",
            "source_instance": self.channel,
            "system_name": self.system_name,
            "names": list(self.names),
            "raw_text": raw_text,
            "confidence": self.confidence,
            "seen_at": self.seen_at,
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
        return ParsedIntelLine(
            channel=channel,
            sender=sender,
            system_name="Unknown",
            raw_text=message,
            seen_at=seen_at,
            names=[],
            hostile_count=None,
            confidence=0.1,
        )

    system = extract_system(message)
    if not system:
        return ParsedIntelLine(
            channel=channel,
            sender=sender,
            system_name="Unknown",
            raw_text=message,
            seen_at=seen_at,
            names=[],
            hostile_count=None,
            confidence=0.2,
        )

    rest = message[len(system):].strip(" :-,;")
    hostile_count = extract_hostile_count(rest)
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
    )


def parse_eve_timestamp(value: str) -> str:
    """Parse an EVE chat timestamp as UTC ISO-8601."""
    dt = datetime.strptime(value, "%Y.%m.%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).isoformat()


def extract_system(message: str) -> str:
    """Return the leading system token from a message."""
    match = SYSTEM_RE.match(message.strip())
    return match.group("system") if match else ""


def extract_hostile_count(message: str) -> int | None:
    """Extract a hostile count from common intel shorthand."""
    match = COUNT_RE.search(message)
    if match:
        raw = match.group("count") or match.group("count2")
        return int(raw)
    if HOSTILE_WORD_RE.search(message):
        return 1
    return None


def extract_names(message: str) -> list[str]:
    """Extract conservative pilot-name candidates after the system token."""
    text = message.strip(" :-,;")
    if not text:
        return []
    if HOSTILE_WORD_RE.search(text) or COUNT_RE.search(text):
        return []
    if NOISE_RE.search(text):
        return []

    names = []
    for chunk in re.split(r"[,;|/]+", text):
        name = chunk.strip()
        if name and len(name) >= 3:
            names.append(name)
    return names
