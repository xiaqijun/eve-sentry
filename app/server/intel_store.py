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

from app.core.models import Observation, ThreatEvent


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
    seen_at: str = field(default_factory=utc_now_iso)
    received_at: str = field(default_factory=utc_now_iso)
    report_id: str = field(default_factory=lambda: uuid4().hex)

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
            "seen_at": self.seen_at,
            "received_at": self.received_at,
            "observation_id": self.report_id,
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
    ) -> None:
        self._filepath = Path(filepath)
        self._systems = dict(DEFAULT_SYSTEMS if systems is None else systems)
        self._links = list(DEFAULT_LINKS if links is None else links)
        self._resolver = resolver
        self._scorer = scorer
        self._lock = threading.RLock()
        self._alert_cache: dict[str, ThreatEvent | None] = {}
        self._reports: list[IntelReport] = self._load_reports()

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
            seen_at=observation.seen_at or utc_now_iso(),
            received_at=observation.received_at or utc_now_iso(),
        )
        with self._lock:
            self._ensure_system(report.system)
            self._reports.append(report)
            self._save_reports()
        return report.to_observation()

    def _enrich_observation(self, observation: Observation) -> Observation:
        """Optionally enrich an observation without blocking ingestion on failure."""
        if self._resolver is None:
            return observation
        try:
            enriched = self._resolver.enrich_observation(observation)
        except Exception:
            return observation
        return enriched if isinstance(enriched, Observation) else observation

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
    ) -> list[dict[str, Any]]:
        """Return generated phase-1 threat events from stored observations."""
        since_query = since.strip() if since else ""
        alerts = []
        for report in self._reports_snapshot():
            alert = self._alert_from_report(report)
            if alert is not None:
                alerts.append(alert.to_dict())

        if since_query:
            alerts = [alert for alert in alerts if alert["created_at"] > since_query]

        alerts.sort(key=lambda item: item["created_at"], reverse=True)
        if limit is not None:
            alerts = alerts[:max(0, limit)]
        return alerts

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
                    alerts.append(alert.to_dict())
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
            alert = self._scorer.score(observation)
        except Exception:
            alert = ThreatEvent.from_observation(observation)
        with self._lock:
            self._alert_cache[report.report_id] = alert
        return alert

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
                    seen_at=str(item.get("seen_at") or utc_now_iso()),
                    received_at=str(
                        item.get("received_at") or item.get("seen_at") or utc_now_iso()
                    ),
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

    def _optional_int(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
