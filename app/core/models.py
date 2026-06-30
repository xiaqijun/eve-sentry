"""Canonical intel data models for the EVE Sentry service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_string(value: Any) -> str:
    """Normalize a user/API supplied string."""
    return str(value or "").strip()


def clean_string_list(values: Any) -> list[str]:
    """Normalize a scalar or list into a unique ordered string list."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = clean_string(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def clean_int_list(values: Any) -> list[int]:
    """Normalize a scalar or list into a unique ordered integer list."""
    if values is None:
        return []
    if isinstance(values, int):
        values = [values]
    if not isinstance(values, list):
        return []

    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.add(number)
            result.append(number)
    return result


@dataclass
class Observation:
    """A normalized sighting or intel item from any collection source."""

    source: str
    system_name: str
    names: list[str] = field(default_factory=list)
    source_instance: str = ""
    system_id: int | None = None
    character_ids: list[int] = field(default_factory=list)
    confidence: float | None = None
    raw_text: str = ""
    seen_at: str = field(default_factory=utc_now_iso)
    received_at: str = field(default_factory=utc_now_iso)
    observation_id: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Observation":
        """Build an observation from API payload data."""
        system_name = (
            clean_string(payload.get("system_name"))
            or clean_string(payload.get("system"))
            or "Unknown"
        )
        confidence = payload.get("confidence")
        if confidence is not None:
            confidence = float(confidence)

        system_id = payload.get("system_id")
        if system_id in {"", None}:
            system_id = None
        elif system_id is not None:
            system_id = int(system_id)

        return cls(
            observation_id=clean_string(payload.get("id"))
            or clean_string(payload.get("observation_id"))
            or uuid4().hex,
            source=clean_string(payload.get("source")) or "api",
            source_instance=clean_string(payload.get("source_instance")),
            system_name=system_name,
            system_id=system_id,
            names=clean_string_list(payload.get("names")),
            character_ids=clean_int_list(payload.get("character_ids")),
            confidence=confidence,
            raw_text=clean_string(payload.get("raw_text") or payload.get("note")),
            seen_at=clean_string(payload.get("seen_at")) or utc_now_iso(),
            received_at=clean_string(payload.get("received_at")) or utc_now_iso(),
        )

    def validate(self) -> None:
        """Validate fields needed for server-side storage."""
        if not self.system_name:
            raise ValueError("system_name must be non-empty")
        if not self.names and not self.character_ids and not self.raw_text:
            raise ValueError(
                "observation must include names, character_ids, or raw_text"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable observation."""
        return {
            "id": self.observation_id,
            "source": self.source,
            "source_instance": self.source_instance,
            "system_name": self.system_name,
            "system_id": self.system_id,
            "names": list(self.names),
            "character_ids": list(self.character_ids),
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "seen_at": self.seen_at,
            "received_at": self.received_at,
        }


@dataclass(frozen=True)
class Evidence:
    """One reason a threat event was generated."""

    evidence_type: str
    weight: int
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.evidence_type,
            "weight": self.weight,
            "summary": self.summary,
        }


@dataclass
class ThreatEvent:
    """A server-generated alert candidate with score and evidence."""

    system_name: str
    names: list[str]
    score: int
    evidence: list[Evidence]
    event_id: str = field(default_factory=lambda: uuid4().hex)
    level: str = ""
    system_id: int | None = None
    character_ids: list[int] = field(default_factory=list)
    source_observation_id: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_observation(cls, observation: Observation) -> "ThreatEvent":
        """Generate the initial MVP threat event from one observation."""
        score = score_observation(observation)
        label = source_label(observation.source)
        names = list(observation.names)
        if not names and observation.character_ids:
            names = [str(item) for item in observation.character_ids]
        if not names and observation.raw_text:
            names = [observation.raw_text]

        evidence = [
            Evidence(
                evidence_type=f"{observation.source}_observed",
                weight=score,
                summary=(
                    f"{label} reported {', '.join(names)} "
                    f"in {observation.system_name}"
                ),
            )
        ]
        return cls(
            event_id=f"evt_{observation.observation_id}",
            system_name=observation.system_name,
            system_id=observation.system_id,
            names=names,
            character_ids=list(observation.character_ids),
            score=score,
            level=threat_level(score),
            evidence=evidence,
            source_observation_id=observation.observation_id,
            created_at=observation.received_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable threat event."""
        return {
            "id": self.event_id,
            "level": self.level or threat_level(self.score),
            "score": self.score,
            "system_name": self.system_name,
            "system": self.system_name,
            "system_id": self.system_id,
            "names": list(self.names),
            "character_ids": list(self.character_ids),
            "evidence": [item.to_dict() for item in self.evidence],
            "source_observation_id": self.source_observation_id,
            "created_at": self.created_at,
            "seen_at": self.created_at,
        }


def source_label(source: str) -> str:
    """Return a readable label for a source id."""
    labels = {
        "local_ocr": "Local OCR",
        "ocr": "Local OCR",
        "eve-sentry-detector": "Local OCR",
        "intel_channel": "Intel channel",
        "manual": "Manual intel",
        "killboard": "Killboard",
        "api": "API",
    }
    return labels.get(source, source or "Intel source")


def score_observation(observation: Observation) -> int:
    """Initial deterministic score for phase-1 alert generation."""
    source = observation.source
    if source in {"local_ocr", "ocr", "eve-sentry-detector"}:
        return 40
    if source == "intel_channel":
        return 30
    if source == "manual":
        return 50
    if source == "killboard":
        return 20
    return 25


def threat_level(score: int) -> str:
    """Map numeric score to the current alert severity."""
    if score >= 100:
        return "critical"
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"

