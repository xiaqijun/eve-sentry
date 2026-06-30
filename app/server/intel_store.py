"""Thread-safe hostile intel store used by the local star-map server."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
)
from app.core.models import Observation, ThreatEvent
from app.intel.scoring import ChannelMention


CHANNEL_SAME_SYSTEM_WINDOW_SECONDS = 10 * 60
CHANNEL_ADJACENT_SYSTEM_WINDOW_SECONDS = 30 * 60
ALERT_LEVEL_RANKS = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class StarSystem:
    """A map node in the lightweight intel map."""

    name: str
    x: float
    y: float
    region: str = "Unknown region"
    security: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "region": self.region,
            "security": self.security,
        }


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
    ) -> None:
        self._filepath = Path(filepath)
        self._systems = dict(DEFAULT_SYSTEMS if systems is None else systems)
        self._links = list(DEFAULT_LINKS if links is None else links)
        self._resolver = resolver
        self._scorer = scorer
        self._enricher = enricher
        self._lock = threading.RLock()
        self._alert_cache: dict[str, ThreatEvent | None] = {}
        self._reports: list[IntelReport] = self._load_reports()

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

        profile = self._call_enricher_profile("character_profile", character_id)
        if profile is None and self._resolver is not None:
            try:
                resolved = self._resolver.character_profile(character_id)
            except Exception:
                resolved = None
            profile = resolved if isinstance(resolved, dict) else None
        if profile is None:
            return None
        return self._profile_result(
            profile,
            id_key="character_id",
            id_value=character_id,
        )

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
        """Return recent killboard activity for one character when enabled."""
        character_id = self._optional_int(character_id)
        if character_id is None or self._enricher is None:
            return None
        try:
            activity = self._enricher.kill_activity(character_id)
        except Exception:
            return None
        return self._activity_result(activity)

    def system_kill_activity(self, system_id: int) -> dict[str, Any] | None:
        """Return recent killboard activity for one solar system when enabled."""
        system_id = self._optional_int(system_id)
        if system_id is None or self._enricher is None:
            return None
        try:
            activity = self._enricher.system_kill_activity(system_id)
        except Exception:
            return None
        return self._activity_result(activity)

    def corporation_kill_activity(
        self,
        corporation_id: int,
    ) -> dict[str, Any] | None:
        """Return recent killboard activity for one corporation when enabled."""
        corporation_id = self._optional_int(corporation_id)
        if corporation_id is None or self._enricher is None:
            return None
        if not hasattr(self._enricher, "corporation_kill_activity"):
            return None
        try:
            activity = self._enricher.corporation_kill_activity(corporation_id)
        except Exception:
            return None
        return self._activity_result(activity)

    def alliance_kill_activity(self, alliance_id: int) -> dict[str, Any] | None:
        """Return recent killboard activity for one alliance when enabled."""
        alliance_id = self._optional_int(alliance_id)
        if alliance_id is None or self._enricher is None:
            return None
        if not hasattr(self._enricher, "alliance_kill_activity"):
            return None
        try:
            activity = self._enricher.alliance_kill_activity(alliance_id)
        except Exception:
            return None
        return self._activity_result(activity)

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
                return duplicate.to_observation()
            self._ensure_system(report.system)
            self._reports.append(report)
            self._save_reports()
        return report.to_observation()

    def _enrich_observation(self, observation: Observation) -> Observation:
        """Optionally enrich an observation without blocking ingestion on failure."""
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
        resolved_system = self._pick_repair_system(observation, candidates, system_matches)
        if resolved_system is None:
            return observation

        repaired_name = str(getattr(resolved_system, "name", "")).strip()
        repaired_id = self._optional_int(getattr(resolved_system, "entity_id", None))
        if not repaired_name or repaired_id is None:
            return observation

        observation.system_name = repaired_name
        observation.system_id = repaired_id
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
                return raw_text[len(prefix):].strip()
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
    ) -> list[dict[str, Any]]:
        """Return recent reports, optionally filtered by system or character."""
        reports = [report.to_dict() for report in self._reports_snapshot()]
        return self._filter_report_like(reports, system=system, name=name, limit=limit)

    def list_observations(
        self,
        source: str | None = None,
        system: str | None = None,
        name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent observations, optionally filtered by source/system/name."""
        source_query = source.strip().casefold() if source else ""
        observations = [
            report.to_observation().to_dict() for report in self._reports_snapshot()
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
            return {
                "alert": alert_data,
                "observation": observation.to_dict(),
                "context": context,
                "explanation": self._alert_explanation(
                    alert_data,
                    observation,
                    context,
                ),
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

    def snapshot(self) -> dict[str, Any]:
        """Return systems, links, reports, observations, alerts, and summary."""
        with self._lock:
            reports = [r.to_dict() for r in self._reports]
            observations = [r.to_observation().to_dict() for r in self._reports]
            alerts = []
            for report in self._reports:
                alert = self._alert_from_report(report)
                if alert is not None:
                    alerts.append(self._alert_to_dict(report, alert))
            system_intel = self._aggregate_by_system(reports)
            character_intel = self._aggregate_by_character(reports)

            systems = []
            for name, system in sorted(self._systems.items()):
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
                    for source, target in self._links
                    if source in self._systems and target in self._systems
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
                    "system_count": len(self._systems),
                    "active_system_count": sum(
                        1 for data in system_intel.values() if data["hostile_count"]
                    ),
                    "report_count": len(reports),
                    "observation_count": len(observations),
                    "alert_count": len(alerts),
                    "hostile_count": len(character_intel),
                },
            }

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
        data["acknowledged"] = bool(report.acknowledged_at)
        data["acknowledged_at"] = report.acknowledged_at
        data["acknowledged_by"] = report.acknowledged_by
        data["acknowledgement_note"] = report.acknowledgement_note
        return data

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
        }

    def _alert_explanation(
        self,
        alert: dict[str, Any],
        observation: Observation,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        reasons = self._alert_reason_summaries(alert)
        context_summaries = self._alert_context_summaries(context)
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
            "sources": self._alert_explanation_sources(
                observation,
                reasons,
                context_summaries,
            ),
        }

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
            summaries.append(
                "Character "
                f"{character_id} has {self._activity_counts(item)}"
                f" in {item.get('window') or 'recent'}"
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
            summaries.append(
                f"{label.title()} {entity_id} has {self._activity_counts(item)}"
                f" in {item.get('window') or 'recent'}"
            )
        return summaries

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
            return kwargs
        try:
            enrichment = self._enricher.enrich(observation)
        except Exception:
            return kwargs

        character_profiles = getattr(enrichment, "character_profiles", None)
        if character_profiles:
            kwargs["character_profiles"] = character_profiles
        kill_activities = getattr(enrichment, "kill_activities", None)
        if kill_activities:
            kwargs["kill_activities"] = kill_activities
        group_activities = getattr(enrichment, "group_activities", None)
        if group_activities:
            kwargs["group_activities"] = group_activities
        return kwargs

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
