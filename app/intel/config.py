"""File-backed intel and scoring configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.intel.classification import CLASSIFICATION_VERSION, ClassificationEngine
from app.intel.scoring import Watchlist


CONFIG_SCHEMA_VERSION = "scoring_config.v1"
EVIDENCE_RULES: tuple[dict[str, Any], ...] = (
    {"type": "local_ocr_seen", "default_weight": 40, "source": "builtin"},
    {"type": "intel_channel_report", "default_weight": 30, "source": "builtin"},
    {"type": "manual_intel", "default_weight": 50, "source": "builtin"},
    {"type": "generic_observation", "default_weight": 25, "source": "builtin"},
    {"type": "blacklist_match", "default_weight": 80, "source": "builtin"},
    {"type": "hostile_corporation", "default_weight": 60, "source": "builtin"},
    {"type": "hostile_alliance", "default_weight": 60, "source": "builtin"},
    {"type": "hostile_standing", "default_weight": 70, "source": "builtin"},
    {
        "type": "intel_channel_same_system_recent",
        "default_weight": 30,
        "source": "builtin",
    },
    {
        "type": "intel_channel_adjacent_system_recent",
        "default_weight": 15,
        "source": "builtin",
    },
)


@dataclass(frozen=True)
class ScoringConfig:
    """User-editable scoring configuration."""

    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    friendly_corporation_ids: list[int] = field(default_factory=list)
    friendly_alliance_ids: list[int] = field(default_factory=list)
    hostile_corporation_ids: list[int] = field(default_factory=list)
    hostile_alliance_ids: list[int] = field(default_factory=list)
    friendly_standing_threshold: float | None = 5.0
    hostile_standing_threshold: float | None = 0.0
    cooldown_seconds: float = 60.0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ScoringConfig":
        return cls(
            whitelist=_clean_string_list(payload.get("whitelist", [])),
            blacklist=_clean_string_list(payload.get("blacklist", [])),
            friendly_corporation_ids=_clean_int_list(
                payload.get("friendly_corporation_ids", [])
            ),
            friendly_alliance_ids=_clean_int_list(
                payload.get("friendly_alliance_ids", [])
            ),
            hostile_corporation_ids=_clean_int_list(
                payload.get("hostile_corporation_ids", [])
            ),
            hostile_alliance_ids=_clean_int_list(
                payload.get("hostile_alliance_ids", [])
            ),
            friendly_standing_threshold=_optional_float(
                payload["friendly_standing_threshold"]
                if "friendly_standing_threshold" in payload
                else 5.0,
                "friendly_standing_threshold",
            ),
            hostile_standing_threshold=_optional_float(
                payload["hostile_standing_threshold"]
                if "hostile_standing_threshold" in payload
                else 0.0,
                "hostile_standing_threshold",
            ),
            cooldown_seconds=_clean_cooldown(payload.get("cooldown_seconds", 60.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "scoring_version": CLASSIFICATION_VERSION,
            "classification_version": CLASSIFICATION_VERSION,
            "defaults": {
                "source": "builtin",
                "friendly_standing_threshold": 5.0,
                "hostile_standing_threshold": 0.0,
                "cooldown_seconds": 60.0,
            },
            "evidence_rules": [dict(item) for item in EVIDENCE_RULES],
            "whitelist": list(self.whitelist),
            "blacklist": list(self.blacklist),
            "friendly_corporation_ids": list(self.friendly_corporation_ids),
            "friendly_alliance_ids": list(self.friendly_alliance_ids),
            "hostile_corporation_ids": list(self.hostile_corporation_ids),
            "hostile_alliance_ids": list(self.hostile_alliance_ids),
            "friendly_standing_threshold": self.friendly_standing_threshold,
            "hostile_standing_threshold": self.hostile_standing_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }

    def to_watchlist(self) -> Watchlist:
        return Watchlist(
            whitelist=set(self.whitelist),
            blacklist=set(self.blacklist),
            friendly_corporation_ids=set(self.friendly_corporation_ids),
            friendly_alliance_ids=set(self.friendly_alliance_ids),
            hostile_corporation_ids=set(self.hostile_corporation_ids),
            hostile_alliance_ids=set(self.hostile_alliance_ids),
            friendly_standing_threshold=self.friendly_standing_threshold,
            hostile_standing_threshold=self.hostile_standing_threshold,
        )

    def build_scorer(self) -> ClassificationEngine:
        return ClassificationEngine(
            watchlist=self.to_watchlist(),
            cooldown_seconds=self.cooldown_seconds,
        )


class IntelConfigStore:
    """JSON-backed scoring configuration store."""

    def __init__(self, path: str | Path = "intel_config.json") -> None:
        self.path = Path(path)
        self._config = self._load()

    @property
    def config(self) -> ScoringConfig:
        return self._config

    def to_dict(self) -> dict[str, Any]:
        return self._config.to_dict()

    def update(self, payload: dict[str, Any]) -> ScoringConfig:
        merged = self.to_dict()
        merged.update(payload)
        config = ScoringConfig.from_payload(merged)
        self._config = config
        self.save()
        return config

    def build_scorer(self) -> ScoringEngine:
        return self._config.build_scorer()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> ScoringConfig:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ScoringConfig()
        except (OSError, json.JSONDecodeError):
            return ScoringConfig()
        if not isinstance(payload, dict):
            return ScoringConfig()
        try:
            return ScoringConfig.from_payload(payload)
        except ValueError:
            return ScoringConfig()


def _clean_string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _clean_int_list(values: Any) -> list[int]:
    if values is None:
        return []
    if isinstance(values, int):
        values = [values]
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.add(number)
            result.append(number)
    return result


def _optional_float(value: Any, label: str) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number or null") from exc


def _clean_cooldown(value: Any) -> float:
    try:
        cooldown = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cooldown_seconds must be a non-negative number") from exc
    if cooldown < 0:
        raise ValueError("cooldown_seconds must be a non-negative number")
    return cooldown
