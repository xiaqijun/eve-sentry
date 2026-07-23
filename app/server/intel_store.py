"""Thread-safe hostile intel store used by the local star-map server."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.channels.parser import (
    extract_direction,
    extract_hostile_count,
    extract_jump_count,
    extract_names,
    extract_system_candidates,
    remove_system,
    strip_inline_sender_prefix,
    strip_repeated_sender_prefix,
)
from app.core.active_intel import (
    ActiveIntelItem,
    ActiveIntelSnapshotResult,
    DEFAULT_OCR_GRACE_SECONDS,
    OCR_MISSING_CONFIRMATIONS,
    channel_ttl_seconds,
    contains_clear_signal,
)
from app.core.models import Observation, ThreatEvent
from app.intel.scoring import ChannelMention
from app.server.esi_worker import EsiWorker


CHANNEL_SAME_SYSTEM_WINDOW_SECONDS = 10 * 60
CHANNEL_ADJACENT_SYSTEM_WINDOW_SECONDS = 30 * 60
ALERT_LEVEL_RANKS = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
STALE_HEARTBEAT_STARTUP_GRACE_SECONDS = 45.0


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ocr_i_l_candidates(name: str) -> list[str]:
    text = str(name or "").strip()
    if not text:
        return []
    positions = [
        index for index, char in enumerate(text)
        if char in {"I", "l"}
    ]
    if not positions:
        return [text]

    candidates = [text]
    seen = {text.casefold()}
    total = 1 << len(positions)
    for mask in range(1, total):
        chars = list(text)
        for bit, position in enumerate(positions):
            if not mask & (1 << bit):
                continue
            chars[position] = "l" if chars[position] == "I" else "I"
        candidate = "".join(chars)
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


@dataclass(frozen=True)
class StarSystem:
    """A map node in the lightweight intel map."""

    name: str
    x: float
    y: float
    region: str = "Unknown region"
    security: float | None = None
    system_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "system_id": self.system_id,
            "x": self.x,
            "y": self.y,
            "region": self.region,
            "security": self.security,
        }


@dataclass(frozen=True)
class _OcrEsiTask:
    active_id: str
    report_id: str
    client_id: str
    original_name: str


@dataclass
class IntelReport:
    """One hostile sighting report.

    This remains the persistence format for compatibility. New phase-1
    observation and alert APIs derive their canonical models from it.
    """

    system: str
    names: list[str]
    source: str = "ocr"
    source_instance: str = ""
    system_id: int | None = None
    character_ids: list[int] = field(default_factory=list)
    confidence: float | None = None
    note: str = ""
    raw_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    seen_at: str = field(default_factory=utc_now_iso)
    received_at: str = field(default_factory=utc_now_iso)
    report_id: str = field(default_factory=lambda: uuid4().hex)
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    acknowledgement_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.report_id,
            "system": self.system,
            "system_name": self.system,
            "system_id": self.system_id,
            "names": list(self.names),
            "character_ids": list(self.character_ids),
            "source": self.source,
            "source_instance": self.source_instance,
            "confidence": self.confidence,
            "note": self.note,
            "raw_text": self.raw_text,
            "metadata": dict(self.metadata),
            "seen_at": self.seen_at,
            "received_at": self.received_at,
            "observation_id": self.report_id,
            "acknowledged": bool(self.acknowledged_at),
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_by": self.acknowledged_by,
            "acknowledgement_note": self.acknowledgement_note,
        }

    def to_observation(self) -> Observation:
        """Return this report as the canonical phase-1 observation model."""
        return Observation(
            observation_id=self.report_id,
            source=self.source,
            source_instance=self.source_instance or self.source,
            system_name=self.system,
            system_id=self.system_id,
            names=list(self.names),
            character_ids=list(self.character_ids),
            confidence=self.confidence,
            raw_text=self.raw_text or self.note,
            metadata=dict(self.metadata),
            seen_at=self.seen_at,
            received_at=self.received_at,
        )


DEFAULT_SYSTEMS: dict[str, StarSystem] = {
    "Jita": StarSystem("Jita", 690, 210, "The Forge", 0.9),
    "Perimeter": StarSystem("Perimeter", 625, 240, "The Forge", 1.0),
    "New Caldari": StarSystem("New Caldari", 570, 200, "The Forge", 1.0),
    "Sobaseki": StarSystem("Sobaseki", 760, 245, "Lonetrek", 0.5),
    "Uedama": StarSystem("Uedama", 505, 335, "The Citadel", 0.5),
    "Tama": StarSystem("Tama", 430, 250, "The Citadel", 0.3),
    "Oijanen": StarSystem("Oijanen", 365, 205, "The Forge", 0.4),
    "Amarr": StarSystem("Amarr", 260, 560, "Domain", 1.0),
    "Niarja": StarSystem("Niarja", 365, 485, "Domain", 0.5),
    "Dodixie": StarSystem("Dodixie", 805, 545, "Sinq Laison", 0.9),
    "Rens": StarSystem("Rens", 170, 250, "Heimatar", 0.9),
    "Hek": StarSystem("Hek", 230, 310, "Metropolis", 0.5),
}


DEFAULT_LINKS: list[tuple[str, str]] = [
    ("Jita", "Perimeter"),
    ("Jita", "Sobaseki"),
    ("Perimeter", "New Caldari"),
    ("Perimeter", "Uedama"),
    ("Uedama", "Tama"),
    ("Tama", "Oijanen"),
    ("Uedama", "Niarja"),
    ("Niarja", "Amarr"),
    ("Niarja", "Dodixie"),
    ("Rens", "Hek"),
    ("Hek", "Tama"),
]


class IntelStore:
    """Stores hostile sightings and derives star-map friendly snapshots."""

    def __init__(
        self,
        filepath: str | Path = "intel_reports.json",
        systems: dict[str, StarSystem] | None = None,
        links: list[tuple[str, str]] | None = None,
        resolver: Any | None = None,
        scorer: Any | None = None,
        enricher: Any | None = None,
        allow_unmapped_systems: bool = True,
    ) -> None:
        self._filepath = Path(filepath)
        self._systems = dict(DEFAULT_SYSTEMS if systems is None else systems)
        self._links = list(DEFAULT_LINKS if links is None else links)
        self._allow_unmapped_systems = bool(allow_unmapped_systems)
        self._resolver = resolver
        self._scorer = scorer
        self._enricher = enricher
        self._lock = threading.RLock()
        self._alert_cache: dict[str, ThreatEvent | None] = {}
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self._stale_heartbeat_cleanup_after = 0.0
        self._active_intel: dict[str, ActiveIntelItem] = {}
        self._ocr_missing_counts: dict[str, int] = {}
        self._ocr_name_corrections: dict[str, str] = {}
        self._character_profile_cache: dict[int, dict[str, Any]] = {}
        self._reports: list[IntelReport] = self._load_reports()
        self._esi_worker = EsiWorker(self._process_ocr_esi_task)

    def wait_for_esi_idle(self, timeout: float | None = None) -> bool:
        """Wait until queued OCR ESI enrichment has finished."""
        return self._esi_worker.wait_idle(timeout=timeout)

    def close(self, *, wait: bool = True) -> None:
        """Stop the dedicated ESI worker."""
        self._esi_worker.close(wait=wait)

    def set_scorer(self, scorer: Any | None) -> None:
        """Replace the alert scorer and force cached alerts to be regenerated."""
        with self._lock:
            self._scorer = scorer
            self._alert_cache.clear()

    def set_enricher(self, enricher: Any | None) -> None:
        """Replace optional alert enrichment and regenerate cached alerts."""
        with self._lock:
            self._enricher = enricher
            self._alert_cache.clear()
            self._character_profile_cache.clear()

    def set_map_data(
        self,
        systems: dict[str, StarSystem],
        links: list[tuple[str, str]],
        allow_unmapped_systems: bool | None = None,
    ) -> None:
        """Replace the configured map topology without touching stored reports."""
        with self._lock:
            self._systems = dict(systems)
            self._links = list(links)
            if allow_unmapped_systems is not None:
                self._allow_unmapped_systems = bool(allow_unmapped_systems)
            for report in self._reports:
                self._ensure_system(report.system)

    def character_by_name(self, name: str) -> dict[str, Any] | None:
        """Resolve a character name and return its public profile when available."""
        resolved = self._resolve_entity_by_name(name, "character")
        if resolved is None:
            return None
        character_id = self._optional_int(getattr(resolved, "entity_id", None))
        if character_id is None:
            return None
        profile = self.character_profile(character_id) or {}
        return self._profile_result(
            profile,
            id_key="character_id",
            id_value=character_id,
            name=str(getattr(resolved, "name", name)).strip(),
        )

    def character_profile(self, character_id: int) -> dict[str, Any] | None:
        """Return a public character profile via optional ESI integration."""
        character_id = self._optional_int(character_id)
        if character_id is None:
            return None
        with self._lock:
            cached = self._character_profile_cache.get(character_id)
            if cached is not None:
                return dict(cached)

        profile = self._call_enricher_profile("character_profile", character_id)
        if profile is None and self._resolver is not None:
            try:
                resolved = self._resolver.character_profile(character_id)
            except Exception:
                resolved = None
            profile = resolved if isinstance(resolved, dict) else None
        if profile is None:
            return None
        result = self._profile_result(
            profile,
            id_key="character_id",
            id_value=character_id,
        )
        with self._lock:
            self._character_profile_cache[character_id] = dict(result)
        return result

    def system_by_name(self, name: str) -> dict[str, Any] | None:
        """Resolve a solar-system name and return its public profile."""
        resolved = self._resolve_entity_by_name(name, "solar_system")
        if resolved is None:
            return None
        system_id = self._optional_int(getattr(resolved, "entity_id", None))
        if system_id is None:
            return None
        profile = self.system_profile(system_id) or {}
        return self._profile_result(
            profile,
            id_key="system_id",
            id_value=system_id,
            name=str(getattr(resolved, "name", name)).strip(),
        )

    def system_profile(self, system_id: int) -> dict[str, Any] | None:
        """Return a public solar-system profile via optional ESI integration."""
        system_id = self._optional_int(system_id)
        if system_id is None:
            return None

        profile = self._call_enricher_profile("system_profile", system_id)
        if profile is None and self._resolver is not None:
            try:
                resolved = self._resolver.system_profile(system_id)
            except Exception:
                resolved = None
            profile = resolved if isinstance(resolved, dict) else None
        if profile is None:
            return None
        return self._profile_result(profile, id_key="system_id", id_value=system_id)

    def character_kill_activity(self, character_id: int) -> dict[str, Any] | None:
        """Return killboard activity when the feature is available."""
        return None

    def system_kill_activity(self, system_id: int) -> dict[str, Any] | None:
        """Return killboard activity when the feature is available."""
        return None

    def corporation_kill_activity(
        self,
        corporation_id: int,
    ) -> dict[str, Any] | None:
        """Return killboard activity when the feature is available."""
        return None

    def alliance_kill_activity(self, alliance_id: int) -> dict[str, Any] | None:
        """Return killboard activity when the feature is available."""
        return None

    def add_report(
        self,
        system: str,
        names: list[str],
        source: str = "ocr",
        confidence: float | None = None,
        note: str = "",
        seen_at: str | None = None,
    ) -> IntelReport:
        """Add a legacy hostile sighting report and persist it."""
        normalized_system = self._normalize_system(system)
        clean_names = self._normalize_names(names)
        if not clean_names:
            raise ValueError("names must contain at least one non-empty value")

        report = IntelReport(
            system=normalized_system,
            names=clean_names,
            source=source.strip() or "ocr",
            confidence=confidence,
            note=note.strip(),
            raw_text=note.strip(),
            seen_at=seen_at or utc_now_iso(),
        )
        with self._lock:
            self._ensure_system(normalized_system)
            self._reports.append(report)
            self._save_reports()
        return report

    def add_observation(self, observation: Observation | dict[str, Any]) -> Observation:
        """Add a canonical observation and persist it."""
        if isinstance(observation, dict):
            observation = Observation.from_payload(observation)

        observation.system_name = self._normalize_system(observation.system_name)
        observation.names = self._normalize_names(observation.names)
        observation.character_ids = self._normalize_ints(observation.character_ids)
        observation = self._enrich_observation(observation)
        observation.validate()

        report = IntelReport(
            report_id=observation.observation_id,
            system=observation.system_name,
            names=observation.names,
            source=observation.source.strip() or "api",
            source_instance=observation.source_instance.strip(),
            system_id=observation.system_id,
            character_ids=observation.character_ids,
            confidence=observation.confidence,
            note=observation.raw_text.strip(),
            raw_text=observation.raw_text.strip(),
            metadata=dict(observation.metadata),
            seen_at=observation.seen_at or utc_now_iso(),
            received_at=observation.received_at or utc_now_iso(),
        )
        with self._lock:
            duplicate = self._find_duplicate_observation(report)
            if duplicate is not None:
                self._apply_channel_active_state(duplicate)
                return duplicate.to_observation()
            self._ensure_system(report.system)
            self._reports.append(report)
            self._apply_channel_active_state(report)
            self._save_reports()
        return report.to_observation()

    def _report_from_observation(self, observation: Observation) -> IntelReport:
        return IntelReport(
            report_id=observation.observation_id,
            system=observation.system_name,
            names=observation.names,
            source=observation.source.strip() or "api",
            source_instance=observation.source_instance.strip(),
            system_id=observation.system_id,
            character_ids=observation.character_ids,
            confidence=observation.confidence,
            note=observation.raw_text.strip(),
            raw_text=observation.raw_text.strip(),
            metadata=dict(observation.metadata),
            seen_at=observation.seen_at or utc_now_iso(),
            received_at=observation.received_at or utc_now_iso(),
        )

    def _build_ocr_observation_report(
        self,
        *,
        source: str,
        source_instance: str,
        system_name: str,
        system_id: int | None,
        client_id: str,
        name: str,
        seen_at: str,
        metadata: dict[str, Any] | None = None,
        enrich: bool = True,
    ) -> tuple[IntelReport, Observation]:
        observation_metadata = {"client_id": client_id}
        observation_metadata.update(metadata or {})
        observation = Observation.from_payload(
            {
                "source": source,
                "source_instance": source_instance,
                "system_name": system_name,
                "system_id": system_id,
                "names": [name],
                "raw_text": name,
                "metadata": observation_metadata,
                "seen_at": seen_at,
            }
        )
        observation.system_name = self._normalize_system(observation.system_name)
        observation.names = self._normalize_names(observation.names)
        observation.character_ids = self._normalize_ints(observation.character_ids)
        if enrich:
            observation = self._enrich_observation(observation)
        else:
            observation.metadata["identity_status"] = "pending"
        observation.validate()
        report = self._report_from_observation(observation)
        return report, observation

    def _process_ocr_esi_task(self, task: _OcrEsiTask) -> None:
        with self._lock:
            current_report = next(
                (
                    report
                    for report in self._reports
                    if report.report_id == task.report_id
                ),
                None,
            )
            if current_report is None:
                return
            observation = current_report.to_observation()

        canonical_name = self._canonicalize_ocr_name(task.original_name)
        observation.names = self._normalize_names([canonical_name])
        observation.raw_text = canonical_name
        observation = self._enrich_observation(observation)
        observation.validate()
        character_profiles = self._character_profiles_for_observation(observation)
        suppressed = self._observation_is_suppressed(
            observation,
            character_profiles=character_profiles,
        )
        checked_at = utc_now_iso()
        observation.metadata["identity_status"] = (
            "resolved" if observation.character_ids else "unresolved"
        )
        observation.metadata["identity_checked_at"] = checked_at
        enriched_report = self._report_from_observation(observation)

        with self._lock:
            report_index = next(
                (
                    index
                    for index, report in enumerate(self._reports)
                    if report.report_id == task.report_id
                ),
                None,
            )
            if report_index is None:
                return
            persisted_report = self._reports[report_index]
            enriched_report.acknowledged_at = persisted_report.acknowledged_at
            enriched_report.acknowledged_by = persisted_report.acknowledged_by
            enriched_report.acknowledgement_note = (
                persisted_report.acknowledgement_note
            )
            self._reports[report_index] = enriched_report
            self._alert_cache.pop(task.report_id, None)

            item = self._active_intel.get(task.active_id)
            if item is None:
                item = next(
                    (
                        candidate
                        for candidate in self._active_intel.values()
                        if task.report_id in candidate.source_observation_ids
                    ),
                    None,
                )

            previous_active_id = task.active_id
            if item is not None:
                previous_active_id = item.active_id
                canonical_active_id = self._active_ocr_id(
                    task.client_id,
                    observation.system_name,
                    canonical_name,
                )
                if canonical_active_id != item.active_id:
                    existing = self._active_intel.get(canonical_active_id)
                    if existing is not None and existing is not item:
                        existing.first_seen_at = min(
                            existing.first_seen_at,
                            item.first_seen_at,
                        )
                        existing.last_seen_at = max(
                            existing.last_seen_at,
                            item.last_seen_at,
                        )
                        existing.active = existing.active or item.active
                        for report_id in item.source_observation_ids:
                            if report_id not in existing.source_observation_ids:
                                existing.source_observation_ids.append(report_id)
                        self._active_intel.pop(item.active_id, None)
                        item = existing
                    else:
                        self._active_intel.pop(item.active_id, None)
                        item.active_id = canonical_active_id
                        self._active_intel[canonical_active_id] = item

                item.name = canonical_name
                item.system_id = observation.system_id
                item.character_id = (
                    observation.character_ids[0]
                    if observation.character_ids
                    else None
                )
                item.metadata = self._active_ocr_metadata(
                    task.client_id,
                    observation,
                    checked_at=checked_at,
                    character_profiles=character_profiles,
                )
                item.metadata["identity_status"] = observation.metadata[
                    "identity_status"
                ]
                if suppressed:
                    item.active = False
                    item.left_at = item.last_seen_at or checked_at

            self._persist_ocr_esi_result(
                enriched_report,
                item,
                previous_active_id=previous_active_id,
            )

    def _apply_hostile_icon_metadata(
        self,
        item: ActiveIntelItem,
        metadata: dict[str, Any],
    ) -> list[IntelReport]:
        """Promote an existing OCR sighting when a later frame verifies a red icon."""
        if not metadata or item.metadata.get("hostile_icon_detected"):
            return []
        item.metadata.update(metadata)
        changed: list[IntelReport] = []
        report_ids = set(item.source_observation_ids)
        for report in self._reports:
            if report.report_id not in report_ids:
                continue
            report.metadata.update(metadata)
            self._alert_cache.pop(report.report_id, None)
            changed.append(report)
        return changed

    def _persist_ocr_esi_result(
        self,
        report: IntelReport,
        item: ActiveIntelItem | None,
        *,
        previous_active_id: str,
    ) -> None:
        self._save_reports()

    def _enrich_observation(self, observation: Observation) -> Observation:
        """Optionally enrich an observation without blocking ingestion on failure."""
        if bool(observation.metadata.get("enrichment_deferred")):
            return observation
        observation = self._repair_channel_observation(observation)
        if self._resolver is None:
            return observation
        try:
            enriched = self._resolver.enrich_observation(observation)
        except Exception:
            return observation
        return enriched if isinstance(enriched, Observation) else observation

    def _repair_channel_observation(self, observation: Observation) -> Observation:
        if self._resolver is None or not hasattr(self._resolver, "resolve_names"):
            return observation
        if observation.source.strip().casefold() != "intel_channel":
            return observation
        if observation.system_name.strip().casefold() != "unknown":
            current_name = observation.system_name.strip()
            if current_name and observation.system_id is not None:
                return observation

        message = self._channel_message_body(observation)
        if not message:
            return observation

        candidates = extract_system_candidates(message)
        if not candidates:
            return observation
        try:
            resolved = self._resolver.resolve_names(candidates)
        except Exception:
            return observation

        system_matches = [
            item for item in resolved
            if str(getattr(item, "category", "")).casefold() == "solar_system"
        ]
        suppressed_names = self._prune_system_chain_names(observation, system_matches)
        repair_status = self._channel_repair_status(observation, system_matches)
        self._record_channel_resolution(
            observation,
            candidates,
            system_matches,
            repair_status,
            suppressed_names=suppressed_names,
        )
        resolved_system = self._pick_repair_system(observation, candidates, system_matches)
        if resolved_system is None:
            return observation

        previous_system_name = observation.system_name.strip()
        repaired_name = str(getattr(resolved_system, "name", "")).strip()
        repaired_id = self._optional_int(getattr(resolved_system, "entity_id", None))
        if not repaired_name or repaired_id is None:
            return observation

        observation.system_name = repaired_name
        observation.system_id = repaired_id
        self._record_channel_resolution(
            observation,
            candidates,
            system_matches,
            "repaired",
            repaired_from=previous_system_name,
            repaired_to=repaired_name,
            suppressed_names=suppressed_names,
        )
        self._reparse_channel_observation(observation, message)
        return observation

    def _channel_message_body(self, observation: Observation) -> str:
        raw_text = observation.raw_text.strip()
        if not raw_text:
            return ""
        sender = str(observation.metadata.get("sender") or "").strip()
        if sender:
            prefix = f"{sender}:"
            if raw_text.startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()
            else:
                stripped = strip_repeated_sender_prefix(raw_text, sender)
                if stripped != raw_text:
                    raw_text = stripped
        inline_stripped = strip_inline_sender_prefix(raw_text)
        if inline_stripped != raw_text:
            return inline_stripped
        if ":" in raw_text:
            _, body = raw_text.split(":", 1)
            return body.strip()
        return raw_text

    def _pick_repair_system(
        self,
        observation: Observation,
        candidates: list[str],
        resolved: list[Any],
    ) -> Any | None:
        query = observation.system_name.strip().casefold()
        matches_by_name: dict[str, Any] = {}
        for item in resolved:
            name = str(getattr(item, "name", "")).strip()
            if not name:
                continue
            matches_by_name[name.casefold()] = item
        if query and query in matches_by_name:
            return None

        matched_candidates = []
        for candidate in candidates:
            item = matches_by_name.get(candidate.casefold())
            if item is not None:
                matched_candidates.append(item)
        unique_by_id: dict[int, Any] = {}
        for item in matched_candidates:
            entity_id = self._optional_int(getattr(item, "entity_id", None))
            if entity_id is not None and entity_id not in unique_by_id:
                unique_by_id[entity_id] = item
        if len(unique_by_id) != 1:
            return None
        return next(iter(unique_by_id.values()))

    def _channel_repair_status(
        self,
        observation: Observation,
        resolved: list[Any],
    ) -> str:
        query = observation.system_name.strip().casefold()
        matched_current = False
        unique_ids: set[int] = set()
        for item in resolved:
            name = str(getattr(item, "name", "")).strip()
            if name and name.casefold() == query:
                matched_current = True
            entity_id = self._optional_int(getattr(item, "entity_id", None))
            if entity_id is not None:
                unique_ids.add(entity_id)
        if matched_current:
            return "validated"
        if len(unique_ids) == 1:
            return "repaired"
        if unique_ids:
            return "ambiguous"
        return "no_match"

    def _prune_system_chain_names(
        self,
        observation: Observation,
        resolved: list[Any],
    ) -> list[str]:
        if not observation.names:
            return []
        system_tokens = self._resolved_system_token_set(resolved)
        if len(system_tokens) < 2:
            return []

        kept_names: list[str] = []
        suppressed_names: list[str] = []
        for name in observation.names:
            if self._is_system_chain_name(name, system_tokens):
                suppressed_names.append(name)
                continue
            kept_names.append(name)
        if suppressed_names:
            observation.names = kept_names
        return suppressed_names

    def _resolved_system_token_set(self, resolved: list[Any]) -> set[str]:
        tokens: set[str] = set()
        for item in resolved:
            name = str(getattr(item, "name", "")).strip()
            if not name:
                continue
            for part in name.split():
                token = part.strip().casefold()
                if token:
                    tokens.add(token)
        return tokens

    def _is_system_chain_name(
        self,
        name: str,
        system_tokens: set[str],
    ) -> bool:
        parts = [part.strip().casefold() for part in name.split() if part.strip()]
        return len(parts) >= 2 and all(part in system_tokens for part in parts)

    def _record_channel_resolution(
        self,
        observation: Observation,
        candidates: list[str],
        resolved: list[Any],
        status: str,
        repaired_from: str = "",
        repaired_to: str = "",
        suppressed_names: list[str] | None = None,
    ) -> None:
        metadata = dict(observation.metadata)
        resolution = self._resolution_metadata_dict(metadata.get("esi_resolution"))
        resolution["candidate_system_names"] = list(candidates)
        resolved_names = self._resolved_system_names(resolved)
        if resolved_names:
            resolution["resolved_system_candidates"] = resolved_names
        else:
            resolution.pop("resolved_system_candidates", None)
        resolution["system_repair_status"] = status
        if repaired_from and repaired_to:
            resolution["system_repaired_from"] = repaired_from
            resolution["system_repaired_to"] = repaired_to
        else:
            resolution.pop("system_repaired_from", None)
            resolution.pop("system_repaired_to", None)
        if suppressed_names:
            resolution["suppressed_name_candidates"] = list(suppressed_names)
        else:
            resolution.pop("suppressed_name_candidates", None)
        metadata["esi_resolution"] = resolution
        observation.metadata = metadata

    def _resolution_metadata_dict(self, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    def _resolved_system_names(self, resolved: list[Any]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for item in resolved:
            name = str(getattr(item, "name", "")).strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    def _reparse_channel_observation(
        self,
        observation: Observation,
        message: str,
    ) -> None:
        rest = remove_system(message, observation.system_name)
        observation.names = self._normalize_names(extract_names(rest))
        metadata = dict(observation.metadata)
        self._set_optional_metadata(
            metadata,
            "hostile_count",
            extract_hostile_count(rest),
        )
        self._set_optional_metadata(
            metadata,
            "jump_count",
            extract_jump_count(rest),
        )
        self._set_optional_metadata(
            metadata,
            "direction",
            extract_direction(rest).strip(),
        )
        observation.metadata = metadata

    def _set_optional_metadata(
        self,
        metadata: dict[str, Any],
        key: str,
        value: Any,
    ) -> None:
        if value in {None, ""}:
            metadata.pop(key, None)
            return
        metadata[key] = value

    def list_reports(
        self,
        system: str | None = None,
        name: str | None = None,
        limit: int | None = None,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recent reports, optionally filtered by system or character."""
        report_items = self._visible_reports(
            self._reports_snapshot(),
            include_suppressed=include_suppressed,
        )
        reports = [report.to_dict() for report in report_items]
        return self._filter_report_like(reports, system=system, name=name, limit=limit)

    def list_observations(
        self,
        source: str | None = None,
        system: str | None = None,
        name: str | None = None,
        limit: int | None = None,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recent observations, optionally filtered by source/system/name."""
        source_query = source.strip().casefold() if source else ""
        report_items = self._visible_reports(
            self._reports_snapshot(),
            include_suppressed=include_suppressed,
        )
        observations = [
            report.to_observation().to_dict() for report in report_items
        ]

        filtered = []
        for observation in observations:
            if source_query and observation["source"].casefold() != source_query:
                continue
            filtered.append(observation)
        return self._filter_report_like(
            filtered,
            system=system,
            name=name,
            limit=limit,
            system_key="system_name",
        )

    def record_ocr_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record one detector OCR snapshot and update active intel state."""
        client_id = str(payload.get("client_id") or "").strip()
        if not client_id:
            raise ValueError("client_id is required")

        source = "eve-sentry-detector"
        source_instance = (
            str(payload.get("source_instance") or client_id).strip() or client_id
        )
        system_name = self._normalize_system(
            str(payload.get("system_name") or payload.get("system") or "")
        )
        system_id = self._optional_int(payload.get("system_id"))
        hostile_icon_count = max(
            0,
            self._optional_int(payload.get("hostile_icon_count")) or 0,
        )
        snapshot_metadata = (
            {
                "hostile_icon_detected": True,
                "hostile_icon_count": hostile_icon_count,
            }
            if hostile_icon_count > 0
            else {}
        )
        defer_esi = self._resolver is not None or self._enricher is not None
        names = self._normalize_ocr_names(
            payload.get("names"),
            resolve=not defer_esi,
        )
        seen_at = self._clean_snapshot_seen_at(payload.get("seen_at"))
        raw_text = ", ".join(names)
        result = ActiveIntelSnapshotResult()
        seen_name_keys = {name.casefold() for name in names}
        changed_reports = False
        esi_tasks: list[_OcrEsiTask] = []
        with self._lock:
            for name in names:
                active_id = self._active_ocr_id(
                    client_id,
                    system_name,
                    name,
                )
                item = self._active_intel.get(active_id)
                if item is None or not item.active:
                    report, observation = self._build_ocr_observation_report(
                        source=source,
                        source_instance=source_instance,
                        system_name=system_name,
                        system_id=system_id,
                        client_id=client_id,
                        name=name,
                        seen_at=seen_at,
                        metadata=snapshot_metadata,
                        enrich=not defer_esi,
                    )
                    duplicate = self._find_duplicate_observation(report)
                    if duplicate is not None:
                        observation = duplicate.to_observation()
                    else:
                        self._ensure_system(report.system)
                        self._reports.append(report)
                        changed_reports = True
                    character_profiles = (
                        []
                        if defer_esi
                        else self._character_profiles_for_observation(observation)
                    )
                    if self._observation_is_suppressed(
                        observation,
                        character_profiles=character_profiles,
                    ):
                        item = self._active_intel.get(active_id)
                        if item is not None and item.active:
                            item.active = False
                            item.left_at = seen_at
                        result.filtered += 1
                        continue
                    self._active_intel[active_id] = ActiveIntelItem(
                        active_id=active_id,
                        source=source,
                        source_instance=source_instance,
                        system_name=system_name,
                        system_id=system_id,
                        character_id=(
                            observation.character_ids[0]
                            if observation.character_ids
                            else None
                        ),
                        target_type="character",
                        name=name,
                        raw_text=raw_text,
                        metadata=(
                            {
                                "client_id": client_id,
                                "identity_status": "pending",
                                **snapshot_metadata,
                            }
                            if defer_esi
                            else self._active_ocr_metadata(
                                client_id,
                                observation,
                                character_profiles=character_profiles,
                            )
                        ),
                        first_seen_at=seen_at,
                        last_seen_at=seen_at,
                        active=True,
                        seen_count=1,
                        source_observation_ids=[observation.observation_id],
                    )
                    if defer_esi:
                        esi_tasks.append(
                            _OcrEsiTask(
                                active_id=active_id,
                                report_id=observation.observation_id,
                                client_id=client_id,
                                original_name=name,
                            )
                        )
                    result.created += 1
                    continue

                elapsed = self._seconds_between_iso(item.last_seen_at, seen_at)
                if elapsed is None or elapsed >= 0:
                    item.last_seen_at = seen_at
                    item.source_instance = source_instance
                    item.raw_text = raw_text
                item.active = True
                item.left_at = ""
                if self._apply_hostile_icon_metadata(item, snapshot_metadata):
                    changed_reports = True
                self._ocr_missing_counts.pop(item.active_id, None)
                result.refreshed += 1

            for item in self._active_intel.values():
                if not item.active:
                    continue
                if item.source != source:
                    continue
                if item.metadata.get("client_id") != client_id:
                    continue
                if item.system_name.casefold() != system_name.casefold():
                    continue
                if item.name.casefold() in seen_name_keys:
                    continue

                elapsed = self._seconds_between_iso(item.last_seen_at, seen_at)
                missing_count = self._ocr_missing_counts.get(item.active_id, 0) + 1
                self._ocr_missing_counts[item.active_id] = missing_count
                if (
                    elapsed is None
                    or elapsed <= DEFAULT_OCR_GRACE_SECONDS
                    or missing_count < OCR_MISSING_CONFIRMATIONS
                ):
                    result.missing += 1
                    continue

                item.active = False
                item.left_at = seen_at
                self._ocr_missing_counts.pop(item.active_id, None)
                self._reset_ocr_alert_cooldown(item)
                result.expired += 1

            result.active = self.list_active_intel(source=source)
            if changed_reports:
                self._save_reports()

        for task in esi_tasks:
            self._esi_worker.submit(task.active_id, task)
        return result.to_dict(include_active=False)

    def list_active_intel(
        self,
        source: str = "",
        system: str = "",
        active: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return active intel items ordered by most recent sighting first."""
        self.expire_active_intel()
        source_query = source.strip().casefold()
        system_query = system.strip().casefold()
        with self._lock:
            items = [item.to_dict() for item in self._active_intel.values()]

        filtered = []
        for item in items:
            if bool(item["active"]) is not active:
                continue
            if source_query and item["source"].casefold() != source_query:
                continue
            if system_query and item["system_name"].casefold() != system_query:
                continue
            filtered.append(item)

        filtered.sort(key=lambda item: item["last_seen_at"], reverse=True)
        if limit is not None:
            filtered = filtered[:max(0, limit)]
        return filtered

    def expire_active_intel(self, now: str | None = None) -> int:
        """Mark active TTL-based intel inactive once its expiry passes."""
        left_at = str(now or utc_now_iso()).strip()
        now_at = self._parse_timestamp(left_at)
        if now_at is None:
            return 0

        expired = 0
        with self._lock:
            for item in self._active_intel.values():
                if not item.active or not item.expires_at:
                    continue
                expires_at = self._parse_timestamp(item.expires_at)
                if expires_at is None or now_at <= expires_at:
                    continue
                item.active = False
                item.left_at = left_at
                self._reset_ocr_alert_cooldown(item)
                expired += 1
            expired += self._expire_stale_detector_ocr_active_intel(left_at)
        return expired

    def list_alerts(
        self,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
        include_since: bool = False,
    ) -> list[dict[str, Any]]:
        """Return generated phase-1 threat events from stored observations."""
        since_query = since.strip() if since else ""
        min_score_value = self._optional_score(min_score)
        min_level_rank = self._alert_level_rank(min_level)
        alerts = []
        for report in self._reports_snapshot():
            alert = self._alert_from_report(report)
            if alert is not None:
                alert_data = self._alert_to_dict(report, alert)
                if self._alert_passes_filters(
                    alert_data,
                    acknowledged=acknowledged,
                    min_score=min_score_value,
                    min_level_rank=min_level_rank,
                ):
                    alerts.append(alert_data)

        if since_query:
            if include_since:
                alerts = [
                    alert for alert in alerts
                    if alert["created_at"] >= since_query
                ]
            else:
                alerts = [
                    alert for alert in alerts
                    if alert["created_at"] > since_query
                ]

        alerts.sort(key=lambda item: item["created_at"], reverse=True)
        if limit is not None:
            alerts = alerts[:max(0, limit)]
        return alerts

    def character_intel(
        self,
        character_id: int,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
    ) -> dict[str, Any] | None:
        """Return observations, alerts, and enrichment for one character."""
        character_id = self._optional_int(character_id)
        if character_id is None:
            return None
        observations = [
            observation
            for observation in self.list_observations(limit=None)
            if character_id in self._normalize_ints(observation.get("character_ids"))
        ]
        alerts = [
            alert
            for alert in self.list_alerts(
                since=since,
                limit=None,
                acknowledged=acknowledged,
                min_score=min_score,
                min_level=min_level,
            )
            if character_id in self._normalize_ints(alert.get("character_ids"))
        ]
        observations = self._limit_recent_items(observations, "seen_at", limit)
        alerts = self._limit_recent_items(alerts, "created_at", limit)
        profile = self.character_profile(character_id)
        return self._intel_payload(
            entity_type="character",
            entity_id=character_id,
            observations=observations,
            alerts=alerts,
            profile=profile,
            activity=self.character_kill_activity(character_id),
            since=since,
            limit=limit,
            acknowledged=acknowledged,
            min_score=min_score,
            min_level=min_level,
        )

    def system_intel(
        self,
        system_id: int,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
    ) -> dict[str, Any] | None:
        """Return observations, alerts, and enrichment for one solar system."""
        system_id = self._optional_int(system_id)
        if system_id is None:
            return None
        observations = [
            observation
            for observation in self.list_observations(limit=None)
            if self._optional_int(observation.get("system_id")) == system_id
        ]
        alerts = [
            alert
            for alert in self.list_alerts(
                since=since,
                limit=None,
                acknowledged=acknowledged,
                min_score=min_score,
                min_level=min_level,
            )
            if self._optional_int(alert.get("system_id")) == system_id
        ]
        observations = self._limit_recent_items(observations, "seen_at", limit)
        alerts = self._limit_recent_items(alerts, "created_at", limit)
        profile = self.system_profile(system_id)
        return self._intel_payload(
            entity_type="system",
            entity_id=system_id,
            observations=observations,
            alerts=alerts,
            profile=profile,
            activity=self.system_kill_activity(system_id),
            since=since,
            limit=limit,
            acknowledged=acknowledged,
            min_score=min_score,
            min_level=min_level,
        )

    def corporation_intel(
        self,
        corporation_id: int,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
    ) -> dict[str, Any] | None:
        """Return observations, alerts, and enrichment for one corporation."""
        corporation_id = self._optional_int(corporation_id)
        if corporation_id is None:
            return None
        observations, alerts = self._intel_by_affiliation(
            "corporation_id",
            corporation_id,
            since=since,
            acknowledged=acknowledged,
            min_score=min_score,
            min_level=min_level,
        )
        observations = self._limit_recent_items(observations, "seen_at", limit)
        alerts = self._limit_recent_items(alerts, "created_at", limit)
        return self._intel_payload(
            entity_type="corporation",
            entity_id=corporation_id,
            observations=observations,
            alerts=alerts,
            profile={"corporation_id": corporation_id},
            activity=self.corporation_kill_activity(corporation_id),
            since=since,
            limit=limit,
            acknowledged=acknowledged,
            min_score=min_score,
            min_level=min_level,
        )

    def alliance_intel(
        self,
        alliance_id: int,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
    ) -> dict[str, Any] | None:
        """Return observations, alerts, and enrichment for one alliance."""
        alliance_id = self._optional_int(alliance_id)
        if alliance_id is None:
            return None
        observations, alerts = self._intel_by_affiliation(
            "alliance_id",
            alliance_id,
            since=since,
            acknowledged=acknowledged,
            min_score=min_score,
            min_level=min_level,
        )
        observations = self._limit_recent_items(observations, "seen_at", limit)
        alerts = self._limit_recent_items(alerts, "created_at", limit)
        return self._intel_payload(
            entity_type="alliance",
            entity_id=alliance_id,
            observations=observations,
            alerts=alerts,
            profile={"alliance_id": alliance_id},
            activity=self.alliance_kill_activity(alliance_id),
            since=since,
            limit=limit,
            acknowledged=acknowledged,
            min_score=min_score,
            min_level=min_level,
        )

    def alert_detail(self, alert_id: str) -> dict[str, Any] | None:
        """Return one alert with its source observation and explanation context."""
        alert_id = str(alert_id or "").strip()
        if not alert_id:
            return None

        for report in self._reports_snapshot():
            alert = self._alert_from_report(report)
            if alert is None:
                continue
            alert_data = self._alert_to_dict(report, alert)
            if not self._alert_matches(alert_id, report, alert_data):
                continue
            observation = report.to_observation()
            context = self._alert_context(observation)
            degraded_sources = self._alert_degraded_sources(observation, context)
            explanation = self._alert_explanation(
                alert_data,
                observation,
                context,
                degraded_sources=degraded_sources,
            )
            return {
                "schema_version": "alert_detail.v1",
                "alert": alert_data,
                "observation": observation.to_dict(),
                "entities": self._alert_entities(observation, context),
                "context": context,
                "explanation": explanation,
            }
        return None

    def alert_cursor(self, alert_id: str) -> str:
        """Return the created_at cursor for an alert id, if it is known."""
        alert_id = str(alert_id or "").strip()
        if not alert_id:
            return ""

        for report in self._reports_snapshot():
            alert = self._alert_from_report(report)
            if alert is None:
                continue
            alert_data = self._alert_to_dict(report, alert)
            if self._alert_matches(alert_id, report, alert_data):
                return str(alert_data.get("created_at") or "")
        return ""

    def ack_alert(
        self,
        alert_id: str,
        acknowledged_by: str = "",
        note: str = "",
        acknowledged_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a generated threat event as acknowledged."""
        alert_id = str(alert_id or "").strip()
        if not alert_id:
            return None

        with self._lock:
            matched_report: IntelReport | None = None
            matched_alert: ThreatEvent | None = None

            for report in self._reports:
                if (
                    report.report_id == alert_id
                    or f"evt_{report.report_id}" == alert_id
                ):
                    matched_report = report
                    break

            if matched_report is None:
                for report in self._reports:
                    alert = self._alert_from_report(report)
                    if alert is not None and alert.event_id == alert_id:
                        matched_report = report
                        matched_alert = alert
                        break

            if matched_report is None:
                return None

            matched_alert = matched_alert or self._alert_from_report(matched_report)
            if matched_alert is None:
                return None

            matched_report.acknowledged_at = (
                str(acknowledged_at or "").strip()
                or matched_report.acknowledged_at
                or utc_now_iso()
            )
            matched_report.acknowledged_by = (
                str(acknowledged_by or "").strip()
                or matched_report.acknowledged_by
            )
            matched_report.acknowledgement_note = (
                str(note or "").strip()
                or matched_report.acknowledgement_note
            )
            self._save_reports()
            return self._alert_to_dict(matched_report, matched_alert)

    def delete_report(self, report_id: str) -> bool:
        """Delete a report by id. Returns True when a report was removed."""
        report_id = report_id.strip()
        if not report_id:
            return False

        with self._lock:
            original_count = len(self._reports)
            self._reports = [
                report for report in self._reports if report.report_id != report_id
            ]
            if len(self._reports) == original_count:
                return False
            self._alert_cache.pop(report_id, None)
            self._save_reports()
            return True

    def record_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record one client heartbeat in memory for runtime diagnostics."""
        client_id = str(payload.get("client_id") or "").strip()
        if not client_id:
            raise ValueError("client_id is required")

        client_type = str(payload.get("client_type") or "client").strip() or "client"
        label = str(payload.get("label") or client_id).strip() or client_id
        status = str(payload.get("status") or "running").strip() or "running"
        seen_at = str(payload.get("seen_at") or utc_now_iso()).strip() or utc_now_iso()
        interval_seconds = self._clean_non_negative_float(
            payload.get("heartbeat_interval_seconds", 0),
            "heartbeat_interval_seconds",
        )
        details = payload.get("details")
        if details is not None and not isinstance(details, dict):
            raise ValueError("details must be a JSON object")

        heartbeat = {
            "client_id": client_id,
            "client_type": client_type,
            "label": label,
            "status": status,
            "seen_at": seen_at,
            "heartbeat_interval_seconds": interval_seconds,
            "details": dict(details or {}),
        }
        with self._lock:
            self._heartbeats[client_id] = heartbeat
        return self._heartbeat_view(heartbeat)

    def list_heartbeats(self) -> list[dict[str, Any]]:
        """Return recent client heartbeat states ordered by newest first."""
        with self._lock:
            items = [self._heartbeat_view(item) for item in self._heartbeats.values()]
        return sorted(items, key=lambda item: str(item.get("seen_at") or ""), reverse=True)

    def heartbeat_snapshot(self) -> dict[str, Any]:
        """Return heartbeat list plus aggregate summary for runtime diagnostics."""
        items = self.list_heartbeats()
        return {
            "heartbeats": items,
            "count": len(items),
            "summary": self._heartbeat_summary_from_items(items),
        }

    def heartbeat_summary(self) -> dict[str, Any]:
        """Return aggregate client heartbeat status for health and dashboards."""
        return self._heartbeat_summary_from_items(self.list_heartbeats())

    def _heartbeat_summary_from_items(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        online_count = 0
        stale_count = 0
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for item in items:
            client_type = str(item.get("client_type") or "client").strip() or "client"
            status = str(item.get("status") or "unknown").strip() or "unknown"
            by_type[client_type] = by_type.get(client_type, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            if item.get("online"):
                online_count += 1
            else:
                stale_count += 1
        return {
            "count": len(items),
            "online_count": online_count,
            "stale_count": stale_count,
            "by_type": by_type,
            "by_status": by_status,
            "latest_seen_at": str(items[0].get("seen_at") or "") if items else "",
            "items": items,
        }

    def _expire_stale_detector_ocr_active_intel(self, left_at: str) -> int:
        if time.monotonic() < self._stale_heartbeat_cleanup_after:
            return 0
        now_at = self._parse_timestamp(left_at)
        if now_at is None:
            return 0
        stale_client_ids: dict[str, str] = {}
        for heartbeat in self._heartbeats.values():
            if str(heartbeat.get("client_type") or "").strip() != "detector_client":
                continue
            view = self._heartbeat_view(heartbeat)
            heartbeat_seen_at = str(view.get("seen_at") or left_at).strip() or left_at
            stale_left_at = self._detector_heartbeat_stale_left_at(view, heartbeat_seen_at)
            if view.get("online"):
                continue
            client_id = str(view.get("client_id") or "").strip()
            if client_id:
                stale_client_ids[client_id] = stale_left_at

        if not stale_client_ids:
            return 0

        expired = 0
        for item in self._active_intel.values():
            if not item.active:
                continue
            if item.source != "eve-sentry-detector":
                continue
            client_id = str(item.metadata.get("client_id") or "").strip()
            stale_seen_at = stale_client_ids.get(client_id)
            if not stale_seen_at:
                for stale_client_id, candidate_seen_at in stale_client_ids.items():
                    if client_id.startswith(f"{stale_client_id}:"):
                        stale_seen_at = candidate_seen_at
                        break
            if not stale_seen_at:
                continue
            if self._channel_seen_after(item.last_seen_at, stale_seen_at):
                last_seen_at = self._parse_timestamp(item.last_seen_at)
                if last_seen_at is None or now_at <= last_seen_at + timedelta(
                    seconds=DEFAULT_OCR_GRACE_SECONDS
                ):
                    continue
                stale_seen_at = (
                    last_seen_at + timedelta(seconds=DEFAULT_OCR_GRACE_SECONDS)
                ).isoformat()
            item.active = False
            item.left_at = stale_seen_at
            self._ocr_missing_counts.pop(item.active_id, None)
            self._reset_ocr_alert_cooldown(item)
            expired += 1
        return expired

    def _reset_ocr_alert_cooldown(self, item: ActiveIntelItem) -> None:
        """Allow a confirmed departed OCR target to alert on its next entry."""
        if item.source != "eve-sentry-detector" or item.target_type != "character":
            return
        reset = getattr(self._scorer, "reset_cooldown", None)
        if callable(reset):
            reset(item.system_name, [item.name])

    def _detector_heartbeat_stale_left_at(
        self,
        heartbeat: dict[str, Any],
        seen_at: str,
    ) -> str:
        try:
            stale_after_seconds = float(heartbeat.get("stale_after_seconds") or 0)
        except (TypeError, ValueError):
            stale_after_seconds = 0.0
        if stale_after_seconds <= 0:
            return seen_at
        parsed = self._parse_timestamp(seen_at)
        if parsed is None:
            return seen_at
        return (parsed + timedelta(seconds=stale_after_seconds)).isoformat()

    def snapshot(self) -> dict[str, Any]:
        """Return systems, links, reports, observations, alerts, and summary."""
        self.expire_active_intel()
        with self._lock:
            report_items = list(self._reports)
            system_items = dict(self._systems)
            link_items = list(self._links)
            heartbeat_count = len(self._heartbeats)
            active_items = [
                item.to_dict() for item in self._active_intel.values() if item.active
            ]

        visible_report_items = self._visible_reports(report_items)
        reports = [report.to_dict() for report in visible_report_items]
        observations = [
            report.to_observation().to_dict() for report in visible_report_items
        ]
        active_source_ids = {
            str(source_id)
            for item in active_items
            for source_id in item.get("source_observation_ids", [])
            if source_id
        }
        alerts = []
        for report in visible_report_items:
            alert = self._alert_from_report(report)
            if alert is not None:
                alert_data = self._alert_to_dict(report, alert)
                source_id = str(
                    alert_data.get("source_observation_id") or report.report_id
                )
                if source_id in active_source_ids:
                    alerts.append(alert_data)
        system_intel = self._aggregate_active_by_system(active_items)
        character_intel = self._aggregate_by_character(reports)

        systems = []
        for name, system in sorted(system_items.items()):
            data = system.to_dict()
            data.update(system_intel.get(name, self._empty_system_intel()))
            if isinstance(data["hostiles"], set):
                data["hostiles"] = sorted(data["hostiles"])
            systems.append(data)

        return {
            "generated_at": utc_now_iso(),
            "systems": systems,
            "links": [
                {"from": source, "to": target}
                for source, target in link_items
                if source in system_items and target in system_items
            ],
            "reports": sorted(
                reports,
                key=lambda report: report["seen_at"],
                reverse=True,
            ),
            "observations": sorted(
                observations,
                key=lambda observation: observation["seen_at"],
                reverse=True,
            ),
            "alerts": sorted(
                alerts,
                key=lambda alert: alert["created_at"],
                reverse=True,
            ),
            "characters": sorted(
                character_intel.values(),
                key=lambda item: item["latest_seen"],
                reverse=True,
            ),
            "summary": {
                "system_count": len(system_items),
                "active_system_count": sum(
                    1
                    for name, data in system_intel.items()
                    if name in system_items and data["hostile_count"]
                ),
                "report_count": len(reports),
                "observation_count": len(observations),
                "alert_count": len(alerts),
                "hostile_count": len(character_intel),
                "heartbeat_count": heartbeat_count,
            },
        }

    def _heartbeat_view(self, heartbeat: dict[str, Any]) -> dict[str, Any]:
        item = {
            "client_id": str(heartbeat.get("client_id") or "").strip(),
            "client_type": str(heartbeat.get("client_type") or "client").strip(),
            "label": str(heartbeat.get("label") or "").strip(),
            "status": str(heartbeat.get("status") or "running").strip(),
            "seen_at": str(heartbeat.get("seen_at") or "").strip(),
            "heartbeat_interval_seconds": self._clean_non_negative_float(
                heartbeat.get("heartbeat_interval_seconds", 0),
                "heartbeat_interval_seconds",
            ),
            "details": dict(heartbeat.get("details") or {}),
        }
        age_seconds = self._heartbeat_age_seconds(item["seen_at"])
        if age_seconds is not None:
            item["age_seconds"] = age_seconds
        stale_after = max(15.0, item["heartbeat_interval_seconds"] * 3.0 or 15.0)
        item["stale_after_seconds"] = stale_after
        item["online"] = age_seconds is not None and age_seconds <= stale_after
        return item

    def _heartbeat_age_seconds(self, seen_at: str) -> float | None:
        seen = self._parse_iso_datetime(seen_at)
        if seen is None:
            return None
        delta = datetime.now(timezone.utc) - seen
        return max(0.0, delta.total_seconds())

    def _parse_iso_datetime(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _clean_non_negative_float(self, value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a non-negative number") from exc
        if number < 0:
            raise ValueError(f"{label} must be a non-negative number")
        return number

    def _reports_snapshot(self) -> list[IntelReport]:
        with self._lock:
            return list(self._reports)

    def _find_duplicate_observation(
        self,
        report: IntelReport,
    ) -> IntelReport | None:
        key = self._observation_dedupe_key(report)
        for existing in reversed(self._reports):
            if existing.report_id == report.report_id:
                return existing
            if key and self._observation_dedupe_key(existing) == key:
                return existing
        return None

    def _observation_dedupe_key(
        self,
        report: IntelReport,
    ) -> tuple[str, str, str, str] | None:
        raw_text = report.raw_text.strip()
        seen_at = report.seen_at.strip()
        if not raw_text or not seen_at:
            return None
        source = (report.source.strip() or "api").casefold()
        source_instance = (report.source_instance.strip() or source).casefold()
        return (source, source_instance, seen_at, raw_text)

    def _alert_from_report(self, report: IntelReport) -> ThreatEvent | None:
        with self._lock:
            if report.report_id in self._alert_cache:
                return self._alert_cache[report.report_id]

        observation = report.to_observation()
        if self._scorer is None:
            alert = ThreatEvent.from_observation(observation)
            with self._lock:
                self._alert_cache[report.report_id] = alert
            return alert
        try:
            kwargs = self._scoring_kwargs(observation)
            alert = self._scorer.score(observation, **kwargs)
        except Exception:
            alert = ThreatEvent.from_observation(observation)
        with self._lock:
            self._alert_cache[report.report_id] = alert
        return alert

    def _alert_to_dict(
        self,
        report: IntelReport,
        alert: ThreatEvent,
    ) -> dict[str, Any]:
        data = alert.to_dict()
        data["verified_characters"] = self._verified_characters_for_report(report)
        data["acknowledged"] = bool(report.acknowledged_at)
        data["acknowledged_at"] = report.acknowledged_at
        data["acknowledged_by"] = report.acknowledged_by
        data["acknowledgement_note"] = report.acknowledgement_note
        return data

    def _verified_characters_for_report(
        self,
        report: IntelReport,
    ) -> list[dict[str, Any]]:
        """Return only character ids paired with names confirmed by ESI."""
        resolution = report.metadata.get("esi_resolution")
        if not isinstance(resolution, dict):
            return []

        character_ids = self._normalize_ints(report.character_ids)
        resolved_names = self._normalize_names(
            resolution.get("resolved_character_names")
        )
        if not character_ids or len(character_ids) != len(resolved_names):
            return []

        return [
            {"character_id": character_id, "name": name}
            for character_id, name in zip(character_ids, resolved_names)
        ]

    def _alert_matches(
        self,
        alert_id: str,
        report: IntelReport,
        alert: dict[str, Any],
    ) -> bool:
        return alert_id in {
            report.report_id,
            f"evt_{report.report_id}",
            str(alert.get("id") or ""),
            str(alert.get("source_observation_id") or ""),
        }

    def _alert_passes_filters(
        self,
        alert: dict[str, Any],
        acknowledged: bool | None,
        min_score: int | None,
        min_level_rank: int | None,
    ) -> bool:
        if acknowledged is not None and bool(alert.get("acknowledged")) != acknowledged:
            return False
        if min_score is not None:
            score = self._optional_score(alert.get("score"))
            if score is None or score < min_score:
                return False
        if min_level_rank is not None:
            level_rank = self._alert_level_rank(str(alert.get("level") or ""))
            if level_rank is None or level_rank < min_level_rank:
                return False
        return True

    def _alert_level_rank(self, value: str | None) -> int | None:
        level = str(value or "").strip().casefold()
        if not level:
            return None
        if level not in ALERT_LEVEL_RANKS:
            raise ValueError(
                "min_level must be one of low, medium, high, or critical"
            )
        return ALERT_LEVEL_RANKS[level]

    def _optional_score(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _intel_by_affiliation(
        self,
        id_key: str,
        entity_id: int,
        since: str | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        observations = []
        alerts = []
        since_query = since.strip() if since else ""
        min_score_value = self._optional_score(min_score)
        min_level_rank = self._alert_level_rank(min_level)

        for report in self._reports_snapshot():
            observation = report.to_observation()
            context = self._alert_context(observation)
            if not self._context_matches_affiliation(context, id_key, entity_id):
                continue

            observations.append(observation.to_dict())
            alert = self._alert_from_report(report)
            if alert is None:
                continue
            alert_data = self._alert_to_dict(report, alert)
            if since_query and alert_data["created_at"] <= since_query:
                continue
            if not self._alert_passes_filters(
                alert_data,
                acknowledged=acknowledged,
                min_score=min_score_value,
                min_level_rank=min_level_rank,
            ):
                continue
            alerts.append(alert_data)
        return observations, alerts

    def _context_matches_affiliation(
        self,
        context: dict[str, Any],
        id_key: str,
        entity_id: int,
    ) -> bool:
        profiles = context.get("character_profiles")
        if isinstance(profiles, list):
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                if self._optional_int(profile.get(id_key)) == entity_id:
                    return True

        entity_type = id_key.removesuffix("_id")
        activities = context.get("group_activities")
        if isinstance(activities, list):
            for activity in activities:
                if not isinstance(activity, dict):
                    continue
                if str(activity.get("entity_type") or "") != entity_type:
                    continue
                activity_id = activity.get("entity_id") or activity.get(id_key)
                if self._optional_int(activity_id) == entity_id:
                    return True
        return False

    def _intel_payload(
        self,
        entity_type: str,
        entity_id: int,
        observations: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        profile: dict[str, Any] | None,
        activity: dict[str, Any] | None,
        since: str | None,
        limit: int | None,
        acknowledged: bool | None,
        min_score: int | None,
        min_level: str | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "intel_entity.v1",
            "entity": {
                "type": entity_type,
                "id": entity_id,
                "profile": profile or {},
            },
            "observations": observations,
            "alerts": alerts,
            "activity": activity or {},
            "counts": {
                "observations": len(observations),
                "alerts": len(alerts),
                "has_activity": bool(activity),
            },
            "filters": {
                "since": since or "",
                "limit": limit,
                "acknowledged": acknowledged,
                "min_score": min_score,
                "min_level": min_level or "",
            },
        }

    def _limit_recent_items(
        self,
        items: list[dict[str, Any]],
        key: str,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        items = sorted(items, key=lambda item: str(item.get(key) or ""), reverse=True)
        if limit is None:
            return items
        return items[:max(0, limit)]

    def _alert_context(self, observation: Observation) -> dict[str, Any]:
        enrichment = self._best_effort_enrichment(observation)
        character_profiles = list(
            getattr(enrichment, "character_profiles", None) or []
        )
        if not character_profiles:
            character_profiles = self._character_profiles_for_observation(observation)

        return {
            "channel_mentions": [
                self._channel_mention_to_dict(mention)
                for mention in self._channel_mentions_for_observation(observation)
            ],
            "character_profiles": character_profiles,
            "kill_activities": [
                item
                for item in (
                    self._activity_result(activity)
                    for activity in getattr(enrichment, "kill_activities", []) or []
                )
                if item is not None
            ],
            "group_activities": [
                item
                for item in (
                    self._activity_result(activity)
                    for activity in getattr(enrichment, "group_activities", []) or []
                )
                if item is not None
            ],
            "resolution": self._alert_resolution_context(observation),
        }

    def _alert_explanation(
        self,
        alert: dict[str, Any],
        observation: Observation,
        context: dict[str, Any],
        degraded_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        reasons = self._alert_reason_summaries(alert)
        context_summaries = self._observation_resolution_summaries(observation)
        context_summaries.extend(self._alert_context_summaries(context))
        names = alert.get("names") if isinstance(alert.get("names"), list) else []
        target = ", ".join(str(name) for name in names if str(name).strip())
        if not target and observation.character_ids:
            target = ", ".join(str(item) for item in observation.character_ids)
        if not target:
            target = observation.raw_text or "Unknown target"

        level = str(alert.get("level") or "low").upper()
        score = alert.get("score")
        score_suffix = f" (score {score})" if score not in {None, ""} else ""
        summary = (
            f"{level} alert for {target} in {observation.system_name}{score_suffix}"
        )
        return {
            "summary": summary,
            "reasons": reasons,
            "context": context_summaries,
            "degraded_sources": list(degraded_sources or []),
            "scoring_version": str(alert.get("scoring_version") or ""),
            "sources": self._alert_explanation_sources(
                observation,
                reasons,
                context_summaries,
            ),
        }

    def _alert_entities(
        self,
        observation: Observation,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        character_ids = self._normalize_ints(observation.character_ids)
        characters = self._entity_profiles(
            context.get("character_profiles"),
            id_key="character_id",
            fallback_ids=character_ids,
        )

        corporations = self._affiliation_entities(
            characters,
            id_key="corporation_id",
            name_key="corporation_name",
        )
        alliances = self._affiliation_entities(
            characters,
            id_key="alliance_id",
            name_key="alliance_name",
        )
        self._merge_group_activity_entities(
            corporations,
            alliances,
            context.get("group_activities"),
        )

        system = {
            "system_id": observation.system_id,
            "name": observation.system_name,
        }
        return {
            "characters": characters,
            "systems": [system] if observation.system_name or observation.system_id else [],
            "corporations": list(corporations.values()),
            "alliances": list(alliances.values()),
        }

    def _entity_profiles(
        self,
        value: Any,
        id_key: str,
        fallback_ids: list[int],
    ) -> list[dict[str, Any]]:
        items = value if isinstance(value, list) else []
        entities: dict[int, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            entity_id = self._optional_int(item.get(id_key))
            if entity_id is None:
                continue
            entity = dict(item)
            entity[id_key] = entity_id
            entities[entity_id] = entity
        for entity_id in fallback_ids:
            entities.setdefault(entity_id, {id_key: entity_id})
        return list(entities.values())

    def _affiliation_entities(
        self,
        characters: list[dict[str, Any]],
        id_key: str,
        name_key: str,
    ) -> dict[int, dict[str, Any]]:
        entities: dict[int, dict[str, Any]] = {}
        for character in characters:
            entity_id = self._optional_int(character.get(id_key))
            if entity_id is None:
                continue
            entity = entities.setdefault(entity_id, {id_key: entity_id})
            name = str(character.get(name_key) or "").strip()
            if name and not entity.get("name"):
                entity["name"] = name
        return entities

    def _merge_group_activity_entities(
        self,
        corporations: dict[int, dict[str, Any]],
        alliances: dict[int, dict[str, Any]],
        value: Any,
    ) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if not isinstance(item, dict):
                continue
            entity_type = str(item.get("entity_type") or "").strip().casefold()
            entity_id = self._optional_int(
                item.get("entity_id") or item.get(f"{entity_type}_id")
            )
            if entity_id is None:
                continue
            if entity_type == "corporation":
                corporations.setdefault(entity_id, {"corporation_id": entity_id})
            elif entity_type == "alliance":
                alliances.setdefault(entity_id, {"alliance_id": entity_id})

    def _alert_resolution_context(self, observation: Observation) -> dict[str, Any]:
        resolution = observation.metadata.get("esi_resolution")
        if not isinstance(resolution, dict):
            return {}
        result = dict(resolution)
        suppressed = resolution.get("suppressed_name_candidates")
        if isinstance(suppressed, list):
            result["suppressed_name_candidates"] = [
                str(item).strip() for item in suppressed if str(item).strip()
            ]
        return result

    def _alert_degraded_sources(
        self,
        observation: Observation,
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        degraded: list[dict[str, Any]] = []
        if observation.character_ids and not context.get("character_profiles"):
            degraded.append(
                {
                    "source": "esi",
                    "reason": "character profiles unavailable",
                }
            )
        if not observation.character_ids and observation.names and self._resolver is None:
            degraded.append(
                {
                    "source": "esi",
                    "reason": "character ids unavailable and ESI resolver disabled",
                }
            )
        return degraded

    def _observation_resolution_summaries(
        self,
        observation: Observation,
    ) -> list[str]:
        resolution = observation.metadata.get("esi_resolution")
        if not isinstance(resolution, dict):
            return []
        summaries: list[str] = []
        suppressed = resolution.get("suppressed_name_candidates")
        if isinstance(suppressed, list):
            suppressed_text = ", ".join(
                str(item).strip() for item in suppressed if str(item).strip()
            )
            if suppressed_text:
                summaries.append(
                    f"ESI suppressed channel name candidates that matched system chains: "
                    f"{suppressed_text}"
                )
        status = str(resolution.get("system_repair_status") or "").strip().casefold()
        if status != "repaired":
            return summaries
        repaired_from = str(resolution.get("system_repaired_from") or "").strip()
        repaired_to = str(
            resolution.get("system_repaired_to") or observation.system_name or ""
        ).strip()
        if not repaired_from or not repaired_to:
            return summaries
        candidates = resolution.get("candidate_system_names")
        if isinstance(candidates, list):
            candidate_text = ", ".join(
                str(item).strip() for item in candidates if str(item).strip()
            )
        else:
            candidate_text = ""
        if candidate_text:
            summaries.append(
                f"ESI repaired channel system {repaired_from} -> {repaired_to} "
                f"from candidates {candidate_text}"
            )
            return summaries
        summaries.append(f"ESI repaired channel system {repaired_from} -> {repaired_to}")
        return summaries

    def _alert_reason_summaries(self, alert: dict[str, Any]) -> list[str]:
        evidence = alert.get("evidence")
        if not isinstance(evidence, list):
            return []
        reasons = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or item.get("type") or "").strip()
            if summary:
                reasons.append(summary)
        return reasons

    def _alert_context_summaries(self, context: dict[str, Any]) -> list[str]:
        summaries: list[str] = []
        summaries.extend(
            self._channel_context_summaries(context.get("channel_mentions"))
        )
        summaries.extend(
            self._profile_context_summaries(context.get("character_profiles"))
        )
        summaries.extend(self._kill_context_summaries(context.get("kill_activities")))
        summaries.extend(self._group_context_summaries(context.get("group_activities")))
        return summaries

    def _channel_context_summaries(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        summaries = []
        for item in value:
            if not isinstance(item, dict):
                continue
            observation = item.get("observation")
            if not isinstance(observation, dict):
                continue
            relation = self._relation_label(str(item.get("relation") or ""))
            system = str(
                observation.get("system_name") or observation.get("system") or "Unknown"
            )
            age = self._age_label(item.get("age_seconds"))
            age_suffix = f" {age}" if age else ""
            summaries.append(f"Recent channel {relation} mention in {system}{age_suffix}")
        return summaries

    def _profile_context_summaries(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        summaries = []
        for item in value:
            if not isinstance(item, dict):
                continue
            label = str(item.get("name") or item.get("character_id") or "").strip()
            if not label:
                continue
            affiliations = []
            corporation_id = item.get("corporation_id")
            alliance_id = item.get("alliance_id")
            if corporation_id not in {None, ""}:
                affiliations.append(f"corp {corporation_id}")
            if alliance_id not in {None, ""}:
                affiliations.append(f"alliance {alliance_id}")
            standing = self._standing_label(
                item.get("contact_standing", item.get("standing"))
            )
            if standing:
                affiliations.append(f"standing {standing}")
            cache_status = str(item.get("cache_status") or "").strip()
            if cache_status:
                affiliations.append(f"cache {cache_status}")
            suffix = f": {', '.join(affiliations)}" if affiliations else ""
            summaries.append(f"ESI profile {label}{suffix}")
        return summaries

    def _standing_label(self, value: Any) -> str:
        if value in {None, ""}:
            return ""
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return str(value).strip()

    def _kill_context_summaries(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        summaries = []
        for item in value:
            if not isinstance(item, dict):
                continue
            character_id = item.get("character_id")
            if character_id in {None, ""}:
                continue
            cache = self._cache_status_label(item)
            cache_suffix = f" ({cache})" if cache else ""
            summaries.append(
                "Character "
                f"{character_id} has {self._activity_counts(item)}"
                f" in {item.get('window') or 'recent'}{cache_suffix}"
            )
        return summaries

    def _group_context_summaries(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        summaries = []
        for item in value:
            if not isinstance(item, dict):
                continue
            entity_type = str(item.get("entity_type") or "group")
            entity_id = item.get("entity_id") or item.get(f"{entity_type}_id")
            if entity_id in {None, ""}:
                continue
            label = entity_type.replace("_", " ")
            cache = self._cache_status_label(item)
            cache_suffix = f" ({cache})" if cache else ""
            summaries.append(
                f"{label.title()} {entity_id} has {self._activity_counts(item)}"
                f" in {item.get('window') or 'recent'}{cache_suffix}"
            )
        return summaries

    def _cache_status_label(self, item: dict[str, Any]) -> str:
        labels = []
        status = str(item.get("cache_status") or "").strip()
        if status:
            labels.append(f"cache {status}")
        request_status = str(item.get("request_status") or "").strip()
        if request_status:
            labels.append(f"request {request_status}")
        return ", ".join(labels)

    def _activity_counts(self, item: dict[str, Any]) -> str:
        parts = []
        if self._has_count(item.get("kills")):
            parts.append(self._plural_count(item["kills"], "kill"))
        if self._has_count(item.get("losses")):
            parts.append(self._plural_count(item["losses"], "loss"))
        return ", ".join(parts) or "activity"

    def _has_count(self, value: Any) -> bool:
        if value in {None, ""}:
            return False
        try:
            return int(value) != 0
        except (TypeError, ValueError):
            return True

    def _alert_explanation_sources(
        self,
        observation: Observation,
        reasons: list[str],
        context_summaries: list[str],
    ) -> list[str]:
        sources = [observation.source]
        if reasons:
            sources.append("scoring")
        if context_summaries:
            sources.append("enrichment")
        return list(dict.fromkeys(source for source in sources if source))

    def _plural_count(self, value: Any, label: str) -> str:
        if str(value) == "1":
            return f"{value} {label}"
        plural = "losses" if label == "loss" else f"{label}s"
        return f"{value} {plural}"

    def _relation_label(self, value: str) -> str:
        relation = value.strip().casefold()
        if relation == "same_system":
            return "same-system"
        if relation == "adjacent_system":
            return "adjacent-system"
        return relation.replace("_", "-") or "related"

    def _age_label(self, value: Any) -> str:
        if value in {None, ""}:
            return ""
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return ""
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"

    def _best_effort_enrichment(self, observation: Observation) -> Any:
        if self._enricher is None:
            return {}
        try:
            return self._enricher.enrich(observation) or {}
        except Exception:
            return {}

    def _character_profiles_for_observation(
        self,
        observation: Observation,
    ) -> list[dict[str, Any]]:
        profiles = []
        for character_id in self._normalize_ints(observation.character_ids):
            profile = self.character_profile(character_id)
            if profile is not None:
                profiles.append(profile)
        return profiles

    def _channel_mention_to_dict(
        self,
        mention: ChannelMention,
    ) -> dict[str, Any]:
        return {
            "relation": mention.relation,
            "age_seconds": mention.age_seconds,
            "observation": mention.observation.to_dict(),
        }

    def _scoring_kwargs(self, observation: Observation) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        channel_mentions = self._channel_mentions_for_observation(observation)
        if channel_mentions:
            kwargs["channel_mentions"] = channel_mentions

        if self._enricher is None:
            character_profiles = self._character_profiles_for_observation(observation)
            if character_profiles:
                kwargs["character_profiles"] = character_profiles
            return kwargs
        try:
            enrichment = self._enricher.enrich(observation)
        except Exception:
            character_profiles = self._character_profiles_for_observation(observation)
            if character_profiles:
                kwargs["character_profiles"] = character_profiles
            return kwargs

        character_profiles = getattr(enrichment, "character_profiles", None)
        if character_profiles:
            kwargs["character_profiles"] = character_profiles
        elif observation.character_ids:
            character_profiles = self._character_profiles_for_observation(observation)
            if character_profiles:
                kwargs["character_profiles"] = character_profiles
        kill_activities = getattr(enrichment, "kill_activities", None)
        if kill_activities:
            kwargs["kill_activities"] = kill_activities
        group_activities = getattr(enrichment, "group_activities", None)
        if group_activities:
            kwargs["group_activities"] = group_activities
        return kwargs

    def _observation_is_suppressed(
        self,
        observation: Observation,
        character_profiles: list[dict[str, Any]] | None = None,
    ) -> bool:
        if self._scorer is None or not hasattr(self._scorer, "suppresses_observation"):
            return False
        profiles = (
            character_profiles
            if character_profiles is not None
            else self._character_profiles_for_observation(observation)
        )
        try:
            return bool(self._scorer.suppresses_observation(observation, None, profiles))
        except Exception:
            return False

    def _active_ocr_metadata(
        self,
        client_id: str,
        observation: Observation,
        checked_at: str | None = None,
        character_profiles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"client_id": client_id}
        for key in ("hostile_icon_detected", "hostile_icon_count"):
            value = observation.metadata.get(key)
            if value not in {None, "", False, 0}:
                metadata[key] = value
        if checked_at:
            metadata["identity_checked_at"] = checked_at
        resolution = observation.metadata.get("esi_resolution")
        if isinstance(resolution, dict):
            metadata["esi_resolution"] = dict(resolution)

        profiles = self._active_character_profile_summaries(
            observation,
            character_profiles=character_profiles,
        )
        if not profiles:
            return metadata

        metadata["character_profiles"] = profiles
        first = profiles[0]
        for key in (
            "character_id",
            "corporation_id",
            "corporation_name",
            "alliance_id",
            "alliance_name",
            "contact_standing",
            "standing",
            "standing_source",
            "standing_contact_id",
            "standing_contact_type",
            "standing_label",
        ):
            value = first.get(key)
            if value not in {None, ""}:
                metadata[key] = value
        return metadata

    def _active_character_profile_summaries(
        self,
        observation: Observation,
        character_profiles: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        profiles = []
        if self._enricher is not None:
            enrichment = self._best_effort_enrichment(observation)
            profiles = list(getattr(enrichment, "character_profiles", None) or [])
        if not profiles:
            profiles = list(character_profiles or [])
        if not profiles:
            profiles = self._character_profiles_for_observation(observation)

        summaries: list[dict[str, Any]] = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            summary = self._active_character_profile_summary(profile)
            if summary:
                summaries.append(summary)
        return summaries

    def _active_character_profile_summary(
        self,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in (
            "character_id",
            "name",
            "corporation_id",
            "corporation_name",
            "alliance_id",
            "alliance_name",
            "security_status",
            "contact_standing",
            "standing",
            "standing_source",
            "standing_contact_id",
            "standing_contact_type",
            "standing_label",
            "cache_status",
            "cached_at",
            "expires_at",
        ):
            value = profile.get(key)
            if value not in {None, ""}:
                summary[key] = value
        return summary

    def _visible_reports(
        self,
        reports: list[IntelReport],
        include_suppressed: bool = False,
    ) -> list[IntelReport]:
        if include_suppressed:
            return list(reports)
        return [
            report for report in reports
            if not self._report_has_whitelisted_names(report)
        ]

    def _report_has_whitelisted_names(self, report: IntelReport) -> bool:
        scorer = self._scorer
        if not bool(getattr(scorer, "suppress_whitelisted_reports", True)):
            return False
        watchlist = getattr(scorer, "watchlist", None)
        whitelist = getattr(watchlist, "whitelist", None)
        if not whitelist:
            return False
        names = self._normalize_names(report.names)
        if not names:
            observation = report.to_observation()
            names = self._normalize_names(observation.names)
        if not names:
            return False
        whitelist_names = {str(name).casefold() for name in whitelist}
        return all(name.casefold() in whitelist_names for name in names)

    def _channel_mentions_for_observation(
        self,
        observation: Observation,
    ) -> list[ChannelMention]:
        if observation.source.strip().casefold() == "intel_channel":
            return []

        observed_at = self._parse_timestamp(observation.seen_at)
        if observed_at is None:
            observed_at = self._parse_timestamp(observation.received_at)
        if observed_at is None:
            return []

        system = observation.system_name.strip()
        adjacent_systems = self._adjacent_systems(system)
        mentions = []
        for report in self._reports_snapshot():
            if report.report_id == observation.observation_id:
                continue
            if report.source.strip().casefold() != "intel_channel":
                continue
            relation = ""
            window_seconds = 0
            if report.system.casefold() == system.casefold():
                relation = "same_system"
                window_seconds = CHANNEL_SAME_SYSTEM_WINDOW_SECONDS
            elif report.system.casefold() in adjacent_systems:
                relation = "adjacent_system"
                window_seconds = CHANNEL_ADJACENT_SYSTEM_WINDOW_SECONDS
            else:
                continue

            mentioned_at = self._parse_timestamp(report.seen_at)
            if mentioned_at is None:
                continue
            age_seconds = (observed_at - mentioned_at).total_seconds()
            if age_seconds < 0 or age_seconds > window_seconds:
                continue
            mentions.append(
                ChannelMention(
                    observation=report.to_observation(),
                    relation=relation,
                    age_seconds=age_seconds,
                )
            )
        return mentions

    def _adjacent_systems(self, system: str) -> set[str]:
        query = system.casefold()
        adjacent: set[str] = set()
        for source, target in self._links:
            if source.casefold() == query:
                adjacent.add(target.casefold())
            if target.casefold() == query:
                adjacent.add(source.casefold())
        return adjacent

    def _parse_timestamp(self, value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _add_seconds_iso(self, timestamp: str, seconds: int) -> str:
        base = self._parse_timestamp(timestamp)
        if base is None:
            base = datetime.now(timezone.utc).replace(microsecond=0)
        return (base + timedelta(seconds=seconds)).isoformat()

    def _active_ocr_id(
        self,
        client_id: str,
        system: str,
        name: str,
    ) -> str:
        key = "\x1f".join(
            [
                client_id.strip().casefold(),
                system.strip().casefold(),
                name.strip().casefold(),
            ]
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"ocr:{digest}"

    def _active_channel_id(
        self,
        source_instance: str,
        system: str,
        raw_text: str,
    ) -> str:
        key = "\x1f".join(
            [
                source_instance.strip().casefold(),
                system.strip().casefold(),
                raw_text.strip().casefold(),
            ]
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"channel:{digest}"

    def _apply_channel_active_state(self, report: IntelReport) -> None:
        if report.source.strip().casefold() != "intel_channel":
            return
        if self._observation_is_suppressed(report.to_observation()):
            return

        seen_at = report.seen_at or report.received_at or utc_now_iso()
        if contains_clear_signal(report.raw_text):
            self._clear_channel_active_state(report, seen_at)
            return

        source_instance = report.source_instance.strip() or report.source
        active_id = self._active_channel_id(
            source_instance,
            report.system,
            report.raw_text,
        )
        expires_at = self._add_seconds_iso(
            seen_at,
            channel_ttl_seconds(report.metadata),
        )
        item = self._active_intel.get(active_id)
        if (
            item is not None
            and not item.active
            and item.cleared_at
            and not self._channel_seen_after(seen_at, item.cleared_at)
        ):
            if report.report_id not in item.source_observation_ids:
                item.source_observation_ids.append(report.report_id)
            return
        if item is None:
            self._active_intel[active_id] = ActiveIntelItem(
                active_id=active_id,
                source="intel_channel",
                source_instance=source_instance,
                system_name=report.system,
                system_id=report.system_id,
                target_type="system",
                raw_text=report.raw_text,
                metadata=dict(report.metadata),
                first_seen_at=seen_at,
                last_seen_at=seen_at,
                expires_at=expires_at,
                active=True,
                seen_count=1,
                confidence=report.confidence,
                source_observation_ids=[report.report_id],
            )
            return

        item.source_instance = source_instance
        item.system_name = report.system
        item.system_id = report.system_id
        item.raw_text = report.raw_text
        item.metadata = dict(report.metadata)
        item.last_seen_at = seen_at
        item.expires_at = expires_at
        item.active = True
        item.cleared_at = ""
        item.seen_count += 1
        item.confidence = report.confidence
        if report.report_id not in item.source_observation_ids:
            item.source_observation_ids.append(report.report_id)

    def _clear_channel_active_state(
        self,
        report: IntelReport,
        cleared_at: str,
    ) -> None:
        source_instance = report.source_instance.strip() or report.source
        system_key = report.system.casefold()
        for item in self._active_intel.values():
            if not item.active:
                continue
            if item.source.casefold() != "intel_channel":
                continue
            if item.source_instance.casefold() != source_instance.casefold():
                continue
            if item.system_name.casefold() != system_key:
                continue
            if not self._channel_seen_after_or_equal(cleared_at, item.last_seen_at):
                continue
            item.active = False
            item.cleared_at = cleared_at

    def _channel_seen_after(self, left: str, right: str) -> bool:
        left_at = self._parse_timestamp(left)
        right_at = self._parse_timestamp(right)
        if left_at is None or right_at is None:
            return False
        return left_at > right_at

    def _channel_seen_after_or_equal(self, left: str, right: str) -> bool:
        left_at = self._parse_timestamp(left)
        right_at = self._parse_timestamp(right)
        if left_at is None or right_at is None:
            return False
        return left_at >= right_at

    def _clean_snapshot_seen_at(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return utc_now_iso()
        if self._parse_timestamp(raw) is None:
            raise ValueError("seen_at must be a valid ISO-8601 timestamp")
        return raw

    def _normalize_ocr_names(
        self,
        names: list[str] | Any,
        *,
        resolve: bool = True,
    ) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for name in self._normalize_names(names):
            if "".join(name.split()).isnumeric():
                continue
            if resolve:
                name = self._canonicalize_ocr_name(name)
            else:
                name = self._ocr_name_corrections.get(name.casefold(), name)
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(name)
        return result

    def _canonicalize_ocr_name(self, name: str) -> str:
        text = str(name or "").strip()
        if not text:
            return text
        cache_key = text.casefold()
        cached = self._ocr_name_corrections.get(cache_key)
        if cached is not None:
            return cached

        candidates = _ocr_i_l_candidates(text)
        canonical = (
            self._resolve_character_name_candidate(candidates)
            or self._resolve_truncated_esi_name(candidates)
            or text
        )
        self._ocr_name_corrections[cache_key] = canonical
        return canonical

    def _resolve_truncated_esi_name(self, candidates: list[str]) -> str | None:
        """Use authenticated ESI search after exact name resolution fails."""
        if self._enricher is None or not hasattr(
            self._enricher, "complete_character_name"
        ):
            return None

        matches: dict[str, str] = {}
        for candidate in candidates:
            candidate_key = candidate.casefold()
            if len(candidate_key) < 8:
                continue
            try:
                completed = self._enricher.complete_character_name(candidate)
            except Exception:
                continue
            completed = str(completed or "").strip()
            completed_key = completed.casefold()
            if (
                len(completed_key) > len(candidate_key)
                and completed_key.startswith(candidate_key)
            ):
                matches[completed_key] = completed

        if len(matches) == 1:
            return next(iter(matches.values()))
        return None

    def _resolve_character_name_candidate(self, candidates: list[str]) -> str | None:
        if self._resolver is None or not hasattr(self._resolver, "resolve_names"):
            return None
        try:
            resolved = self._resolver.resolve_names(candidates)
        except Exception:
            return None
        matches: dict[str, Any] = {}
        for item in resolved:
            if str(getattr(item, "category", "")).casefold() != "character":
                continue
            item_name = str(getattr(item, "name", "")).strip()
            if not item_name:
                continue
            for candidate in candidates:
                if item_name.casefold() == candidate.casefold():
                    matches[candidate.casefold()] = item

        if not matches:
            return None

        original_key = candidates[0].casefold()
        if original_key in matches:
            selected = matches[original_key]
        elif len(matches) == 1:
            selected = next(iter(matches.values()))
        else:
            return None

        character_id = self._optional_int(getattr(selected, "entity_id", None))
        if character_id is not None:
            self.character_profile(character_id)
        return str(getattr(selected, "name", "")).strip() or None

    def _seconds_between_iso(self, left: str, right: str) -> float | None:
        left_at = self._parse_timestamp(left)
        right_at = self._parse_timestamp(right)
        if left_at is None or right_at is None:
            return None
        return (right_at - left_at).total_seconds()

    def _resolve_entity_by_name(self, name: str, category: str) -> Any | None:
        if self._resolver is None:
            return None
        query = str(name or "").strip()
        if not query:
            return None
        target_category = category.casefold()
        try:
            resolved = self._resolver.resolve_names([query])
        except Exception:
            return None
        for item in resolved:
            if str(getattr(item, "category", "")).casefold() == target_category:
                return item
        return None

    def _call_enricher_profile(
        self,
        method_name: str,
        entity_id: int,
    ) -> dict[str, Any] | None:
        if self._enricher is None or not hasattr(self._enricher, method_name):
            return None
        try:
            profile = getattr(self._enricher, method_name)(entity_id)
        except Exception:
            return None
        return profile if isinstance(profile, dict) else None

    def _profile_result(
        self,
        profile: dict[str, Any],
        id_key: str,
        id_value: int,
        name: str = "",
    ) -> dict[str, Any]:
        result = dict(profile)
        result[id_key] = int(id_value)
        if name and not result.get("name"):
            result["name"] = name
        return result

    def _activity_result(self, activity: Any | None) -> dict[str, Any] | None:
        if activity is None:
            return None
        if isinstance(activity, dict):
            return dict(activity)
        if hasattr(activity, "to_dict"):
            result = activity.to_dict()
            return result if isinstance(result, dict) else None
        return None

    def _filter_report_like(
        self,
        items: list[dict[str, Any]],
        system: str | None = None,
        name: str | None = None,
        limit: int | None = None,
        system_key: str = "system",
    ) -> list[dict[str, Any]]:
        system_query = system.strip().casefold() if system else ""
        name_query = name.strip().casefold() if name else ""

        filtered = []
        for item in items:
            if system_query and item[system_key].casefold() != system_query:
                continue
            if name_query and not any(
                value.casefold() == name_query for value in item["names"]
            ):
                continue
            filtered.append(item)

        filtered.sort(key=lambda item: item["seen_at"], reverse=True)
        if limit is not None:
            filtered = filtered[:max(0, limit)]
        return filtered

    def _load_reports(self) -> list[IntelReport]:
        try:
            if not self._filepath.exists():
                return []
            raw = json.loads(self._filepath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        reports: list[IntelReport] = []
        if not isinstance(raw, list):
            return reports
        for item in raw:
            if not isinstance(item, dict):
                continue
            names = self._normalize_names(item.get("names", []))
            system = self._normalize_system(str(item.get("system", "")))
            raw_text = str(item.get("raw_text") or item.get("note") or "")
            character_ids = self._normalize_ints(item.get("character_ids"))
            metadata = self._normalize_metadata(item.get("metadata"))
            for key in ("hostile_count", "sender", "channel", "jump_count", "direction"):
                value = item.get(key)
                if value is not None and value != "" and key not in metadata:
                    metadata[key] = value
            if not system or (not names and not raw_text and not character_ids):
                continue
            reports.append(
                IntelReport(
                    report_id=str(item.get("id") or uuid4().hex),
                    system=system,
                    names=names,
                    source=str(item.get("source") or "ocr"),
                    source_instance=str(item.get("source_instance") or ""),
                    system_id=self._optional_int(item.get("system_id")),
                    character_ids=character_ids,
                    confidence=item.get("confidence"),
                    note=str(item.get("note") or ""),
                    raw_text=raw_text,
                    metadata=metadata,
                    seen_at=str(item.get("seen_at") or utc_now_iso()),
                    received_at=str(
                        item.get("received_at") or item.get("seen_at") or utc_now_iso()
                    ),
                    acknowledged_at=str(item.get("acknowledged_at") or ""),
                    acknowledged_by=str(item.get("acknowledged_by") or ""),
                    acknowledgement_note=str(item.get("acknowledgement_note") or ""),
                )
            )
            self._ensure_system(system)
        return reports

    def _save_reports(self) -> None:
        self._filepath.write_text(
            json.dumps(
                [report.to_dict() for report in self._reports],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _ensure_system(self, system: str) -> None:
        if system in self._systems:
            return
        if not self._allow_unmapped_systems:
            return
        self._systems[system] = self._generated_system(system)

    def _generated_system(self, system: str) -> StarSystem:
        digest = hashlib.sha256(system.encode("utf-8")).digest()
        x = 90 + int.from_bytes(digest[:2], "big") / 65535 * 820
        y = 90 + int.from_bytes(digest[2:4], "big") / 65535 * 520
        return StarSystem(system, round(x, 1), round(y, 1), "Unmapped", None)

    def _aggregate_by_system(
        self,
        reports: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        intel: dict[str, dict[str, Any]] = {}
        for report in reports:
            system = report["system"]
            entry = intel.setdefault(system, self._empty_system_intel())
            names = set(report["names"])
            entry["hostiles"].update(names)
            entry["hostile_count"] = len(entry["hostiles"])
            entry["latest_seen"] = max(
                entry["latest_seen"] or report["seen_at"],
                report["seen_at"],
            )
            entry["report_count"] += 1

        for entry in intel.values():
            entry["hostiles"] = sorted(entry["hostiles"])
        return intel

    def _aggregate_active_by_system(
        self,
        active_items: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        intel: dict[str, dict[str, Any]] = {}
        for item in active_items:
            system = str(item.get("system_name") or "").strip()
            if not system:
                continue
            entry = intel.setdefault(system, self._empty_system_intel())
            name = str(item.get("name") or "").strip()
            raw_text = str(item.get("raw_text") or "").strip()
            label = name or raw_text
            is_hostile = self._active_item_is_hostile(item)
            if label and is_hostile:
                entry["hostiles"].add(label)
            entry["latest_seen"] = max(
                entry["latest_seen"] or item["last_seen_at"],
                item["last_seen_at"],
            )
            if is_hostile:
                entry["report_count"] += max(1, int(item.get("seen_count") or 1))
            metadata = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            hostile_count = metadata.get("hostile_count")
            if is_hostile and isinstance(hostile_count, int) and hostile_count > 0:
                entry["hostile_count"] += hostile_count
            elif is_hostile and label:
                entry["hostile_count"] += 1

        for entry in intel.values():
            entry["hostiles"] = sorted(entry["hostiles"])
        return intel

    def _active_item_is_hostile(self, item: dict[str, Any]) -> bool:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        hostile_count = metadata.get("hostile_count")
        if isinstance(hostile_count, int) and hostile_count > 0:
            return True

        source = str(item.get("source") or "").strip().casefold()
        if source not in {"local_ocr", "ocr", "eve-sentry-detector"}:
            return False

        standing = self._optional_float(
            metadata.get("contact_standing", metadata.get("standing"))
        )
        if standing is None:
            return False
        scorer = self._scorer
        watchlist = getattr(scorer, "watchlist", None)
        friendly_threshold = getattr(watchlist, "friendly_standing_threshold", 5.0)
        hostile_threshold = getattr(watchlist, "hostile_standing_threshold", 0.0)
        if friendly_threshold is not None and standing >= float(friendly_threshold):
            return False
        return hostile_threshold is not None and standing <= float(hostile_threshold)

    def _aggregate_by_character(
        self,
        reports: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        intel: dict[str, dict[str, Any]] = {}
        for report in reports:
            for name in report["names"]:
                entry = intel.setdefault(
                    name,
                    {
                        "name": name,
                        "systems": set(),
                        "first_seen": report["seen_at"],
                        "latest_seen": report["seen_at"],
                        "sighting_count": 0,
                    },
                )
                entry["systems"].add(report["system"])
                entry["first_seen"] = min(entry["first_seen"], report["seen_at"])
                entry["latest_seen"] = max(entry["latest_seen"], report["seen_at"])
                entry["sighting_count"] += 1

        for entry in intel.values():
            entry["systems"] = sorted(entry["systems"])
        return intel

    def _empty_system_intel(self) -> dict[str, Any]:
        return {
            "hostiles": set(),
            "hostile_count": 0,
            "latest_seen": None,
            "report_count": 0,
        }

    def _normalize_system(self, system: str) -> str:
        return system.strip() or "Unknown"

    def _normalize_names(self, names: list[str] | Any) -> list[str]:
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            return []
        seen: set[str] = set()
        result: list[str] = []
        for name in names:
            text = str(name).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result

    def _normalize_ints(self, values: Any) -> list[int]:
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

    def _normalize_metadata(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key).strip()
            if text:
                result[text] = item
        return result

    def _optional_int(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _optional_float(self, value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
