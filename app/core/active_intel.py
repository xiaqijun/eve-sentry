"""Realtime active intel state derived from historical observations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


OCR_SOURCES = {"local_ocr", "ocr", "eve-sentry-detector"}
DEFAULT_OCR_GRACE_SECONDS = 6

CLEAR_WORDS = (
    "clr",
    "clear",
    "clean",
    "安全",
    "清了",
    "已清",
    "走了",
    "没了",
    "散了",
)


@dataclass
class ActiveIntelItem:
    active_id: str
    source: str
    source_instance: str
    system_name: str
    target_type: str = "character"
    name: str = ""
    system_id: int | None = None
    character_id: int | None = None
    raw_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    first_seen_at: str = ""
    last_seen_at: str = ""
    expires_at: str = ""
    left_at: str = ""
    cleared_at: str = ""
    active: bool = True
    seen_count: int = 1
    confidence: float | None = None
    source_observation_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.active_id,
            "source": self.source,
            "source_instance": self.source_instance,
            "system_name": self.system_name,
            "system_id": self.system_id,
            "target_type": self.target_type,
            "name": self.name,
            "character_id": self.character_id,
            "raw_text": self.raw_text,
            "metadata": dict(self.metadata),
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "expires_at": self.expires_at,
            "left_at": self.left_at,
            "cleared_at": self.cleared_at,
            "active": self.active,
            "seen_count": self.seen_count,
            "confidence": self.confidence,
            "source_observation_ids": list(self.source_observation_ids),
        }


@dataclass
class ActiveIntelSnapshotResult:
    created: int = 0
    refreshed: int = 0
    missing: int = 0
    expired: int = 0
    filtered: int = 0
    active: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "created": self.created,
            "refreshed": self.refreshed,
            "missing": self.missing,
            "expired": self.expired,
            "filtered": self.filtered,
            "active": list(self.active),
        }


def contains_clear_signal(text: str) -> bool:
    haystack = str(text or "").casefold()
    for word in CLEAR_WORDS:
        token = word.casefold()
        if re.fullmatch(r"[a-z]+", token):
            if re.search(rf"\b{re.escape(token)}\b", haystack):
                return True
        elif token in haystack:
            return True
    return False


def channel_ttl_seconds(metadata: dict[str, Any]) -> int:
    if _truthy(metadata.get("bridge")) or _truthy(metadata.get("staging")):
        return 1200
    if _truthy(metadata.get("fleet")) or _truthy(metadata.get("camp")):
        return 900
    if _positive_int(metadata.get("jump_count")) is not None:
        return 300
    if _positive_int(metadata.get("hostile_count")) is not None:
        return 180
    return 600


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
