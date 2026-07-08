"""SQLite-backed hostile intel store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.core.active_intel import ActiveIntelItem
from app.core.models import Observation
from app.server.intel_store import IntelReport, IntelStore, StarSystem, utc_now_iso


class _ClosingConnection(sqlite3.Connection):
    """SQLite connection that closes when used as a context manager."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class SQLiteIntelStore(IntelStore):
    """Persist intel reports in SQLite while keeping the IntelStore API."""

    def __init__(
        self,
        db_path: str | Path = "intel.sqlite3",
        import_json_path: str | Path | None = None,
        systems: dict[str, StarSystem] | None = None,
        links: list[tuple[str, str]] | None = None,
        resolver: Any | None = None,
        scorer: Any | None = None,
        enricher: Any | None = None,
        allow_unmapped_systems: bool = True,
    ) -> None:
        self._db_path = Path(db_path)
        self._import_json_path = Path(import_json_path) if import_json_path else None
        super().__init__(
            filepath=self._db_path,
            systems=systems,
            links=links,
            resolver=resolver,
            scorer=scorer,
            enricher=enricher,
            allow_unmapped_systems=allow_unmapped_systems,
        )
        self._active_intel = self._read_active_intel()
        self._heartbeats = self._read_heartbeats()

    def _load_reports(self) -> list[IntelReport]:
        self._migrate()
        reports = self._read_reports()
        if reports or self._import_json_path is None:
            return reports
        if self._meta_value("legacy_json_imported") == "1":
            return reports
        if not self._import_json_path.exists():
            return reports

        legacy = IntelStore(
            self._import_json_path,
            systems={},
            links=[],
            resolver=self._resolver,
        )._reports_snapshot()
        if legacy:
            self._replace_reports(legacy)
        self._set_meta("legacy_json_imported", "1")
        return legacy

    def _save_reports(self) -> None:
        self._replace_reports(self._reports)

    def add_report(
        self,
        system: str,
        names: list[str],
        source: str = "ocr",
        confidence: float | None = None,
        note: str = "",
        seen_at: str | None = None,
    ) -> IntelReport:
        """Add a legacy hostile sighting report and persist only that row."""
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
            self._upsert_report(report)
        return report

    def add_observation(self, observation: Observation | dict[str, Any]) -> Observation:
        """Add a canonical observation and persist only that row."""
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
                self._replace_active_intel()
                return duplicate.to_observation()
            self._ensure_system(report.system)
            self._reports.append(report)
            self._apply_channel_active_state(report)
            self._upsert_report(report)
            self._replace_active_intel()
        return report.to_observation()

    def record_ocr_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record an OCR snapshot and persist derived active intel state."""
        result = super().record_ocr_snapshot(payload)
        self._replace_active_intel()
        return result

    def expire_active_intel(self, now: str | None = None) -> int:
        """Expire TTL-based active intel and persist changed rows."""
        expired = super().expire_active_intel(now)
        if expired:
            self._replace_active_intel()
        return expired

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
            matched_alert = None

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
            self._upsert_report(matched_report)
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
            self._delete_report(report_id)
            return True

    def _migrate(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intel_reports (
                    report_id TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    names_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_instance TEXT NOT NULL,
                    system_id INTEGER,
                    character_ids_json TEXT NOT NULL,
                    confidence REAL,
                    note TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    seen_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL DEFAULT '',
                    acknowledged_by TEXT NOT NULL DEFAULT '',
                    acknowledgement_note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_column(
                connection,
                "intel_reports",
                "metadata_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "intel_reports",
                "acknowledged_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "intel_reports",
                "acknowledged_by",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "intel_reports",
                "acknowledgement_note",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intel_reports_seen_at
                ON intel_reports(seen_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intel_reports_system
                ON intel_reports(system)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS active_intel (
                    active_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_instance TEXT NOT NULL,
                    system TEXT NOT NULL,
                    system_id INTEGER,
                    target_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    character_id INTEGER,
                    raw_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL DEFAULT '',
                    left_at TEXT NOT NULL DEFAULT '',
                    cleared_at TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    confidence REAL,
                    source_observation_ids_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            self._ensure_column(
                connection,
                "active_intel",
                "active_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "source",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "source_instance",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "system",
                "TEXT NOT NULL DEFAULT 'Unknown'",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "system_id",
                "INTEGER",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "target_type",
                "TEXT NOT NULL DEFAULT 'character'",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "name",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "character_id",
                "INTEGER",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "raw_text",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "metadata_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "first_seen_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "last_seen_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "expires_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "left_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "cleared_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "active",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "seen_count",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "confidence",
                "REAL",
            )
            self._ensure_column(
                connection,
                "active_intel",
                "source_observation_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS client_heartbeats (
                    client_id TEXT PRIMARY KEY,
                    client_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    heartbeat_interval_seconds REAL NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._ensure_column(
                connection,
                "client_heartbeats",
                "heartbeat_interval_seconds",
                "REAL NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "client_heartbeats",
                "details_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_client_heartbeats_seen_at
                ON client_heartbeats(seen_at)
                """
            )

    def _read_reports(self) -> list[IntelReport]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT report_id, system, names_json, source, source_instance,
                       system_id, character_ids_json, confidence, note, raw_text,
                       metadata_json, seen_at, received_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note
                FROM intel_reports
                ORDER BY seen_at ASC, received_at ASC
                """
            ).fetchall()

        reports = []
        for row in rows:
            report = self._report_from_row(row)
            if report is not None:
                reports.append(report)
                self._ensure_system(report.system)
        return reports

    def _read_active_intel(self) -> dict[str, ActiveIntelItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT active_id, source, source_instance, system, system_id,
                       target_type, name, character_id, raw_text, metadata_json,
                       first_seen_at, last_seen_at, expires_at, left_at,
                       cleared_at, active, seen_count, confidence,
                       source_observation_ids_json
                FROM active_intel
                ORDER BY last_seen_at ASC
                """
            ).fetchall()

        active: dict[str, ActiveIntelItem] = {}
        for row in rows:
            item = self._active_item_from_row(row)
            if item is not None:
                active[item.active_id] = item
                self._ensure_system(item.system_name)
        return active

    def _replace_reports(self, reports: list[IntelReport]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM intel_reports")
            connection.executemany(
                """
                INSERT INTO intel_reports (
                    report_id, system, names_json, source, source_instance,
                    system_id, character_ids_json, confidence, note, raw_text,
                    metadata_json, seen_at, received_at, acknowledged_at,
                    acknowledged_by, acknowledgement_note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._row_from_report(report) for report in reports],
            )

    def _replace_active_intel(self) -> None:
        with self._lock:
            rows = [self._active_row(item) for item in self._active_intel.values()]
        with self._connect() as connection:
            connection.execute("DELETE FROM active_intel")
            connection.executemany(
                """
                INSERT INTO active_intel (
                    active_id, source, source_instance, system, system_id,
                    target_type, name, character_id, raw_text, metadata_json,
                    first_seen_at, last_seen_at, expires_at, left_at,
                    cleared_at, active, seen_count, confidence,
                    source_observation_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _upsert_report(self, report: IntelReport) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO intel_reports (
                    report_id, system, names_json, source, source_instance,
                    system_id, character_ids_json, confidence, note, raw_text,
                    metadata_json, seen_at, received_at, acknowledged_at,
                    acknowledged_by, acknowledgement_note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    system = excluded.system,
                    names_json = excluded.names_json,
                    source = excluded.source,
                    source_instance = excluded.source_instance,
                    system_id = excluded.system_id,
                    character_ids_json = excluded.character_ids_json,
                    confidence = excluded.confidence,
                    note = excluded.note,
                    raw_text = excluded.raw_text,
                    metadata_json = excluded.metadata_json,
                    seen_at = excluded.seen_at,
                    received_at = excluded.received_at,
                    acknowledged_at = excluded.acknowledged_at,
                    acknowledged_by = excluded.acknowledged_by,
                    acknowledgement_note = excluded.acknowledgement_note
                """,
                self._row_from_report(report),
            )

    def _delete_report(self, report_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM intel_reports WHERE report_id = ?",
                (report_id,),
            )

    def record_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist heartbeats in SQLite while keeping the base in-memory cache."""
        heartbeat = super().record_heartbeat(payload)
        raw = {
            "client_id": heartbeat["client_id"],
            "client_type": heartbeat["client_type"],
            "label": heartbeat["label"],
            "status": heartbeat["status"],
            "seen_at": heartbeat["seen_at"],
            "heartbeat_interval_seconds": heartbeat["heartbeat_interval_seconds"],
            "details": dict(heartbeat.get("details") or {}),
        }
        self._write_heartbeat(raw)
        if raw["client_type"] == "detector_client":
            self._replace_active_intel()
        return heartbeat

    def _read_heartbeats(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT client_id, client_type, label, status, seen_at,
                       heartbeat_interval_seconds, details_json
                FROM client_heartbeats
                ORDER BY seen_at DESC
                """
            ).fetchall()

        heartbeats: dict[str, dict[str, Any]] = {}
        for row in rows:
            heartbeat = self._heartbeat_from_row(row)
            if heartbeat is not None:
                heartbeats[heartbeat["client_id"]] = heartbeat
        return heartbeats

    def _write_heartbeat(self, heartbeat: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO client_heartbeats (
                    client_id,
                    client_type,
                    label,
                    status,
                    seen_at,
                    heartbeat_interval_seconds,
                    details_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_type = excluded.client_type,
                    label = excluded.label,
                    status = excluded.status,
                    seen_at = excluded.seen_at,
                    heartbeat_interval_seconds = excluded.heartbeat_interval_seconds,
                    details_json = excluded.details_json
                """,
                self._heartbeat_row(heartbeat),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def _meta_value(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM store_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else ""

    def _set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO store_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if column in {str(row["name"]) for row in rows}:
            return
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _report_from_row(self, row: sqlite3.Row) -> IntelReport | None:
        try:
            names = self._normalize_names(json.loads(str(row["names_json"])))
            character_ids = self._normalize_ints(
                json.loads(str(row["character_ids_json"]))
            )
            metadata = self._normalize_metadata(json.loads(str(row["metadata_json"])))
        except json.JSONDecodeError:
            return None
        system = self._normalize_system(str(row["system"]))
        if not system or (
            not names and not character_ids and not str(row["raw_text"] or "")
        ):
            return None
        return IntelReport(
            report_id=str(row["report_id"]),
            system=system,
            names=names,
            source=str(row["source"] or "ocr"),
            source_instance=str(row["source_instance"] or ""),
            system_id=self._optional_int(row["system_id"]),
            character_ids=character_ids,
            confidence=row["confidence"],
            note=str(row["note"] or ""),
            raw_text=str(row["raw_text"] or ""),
            metadata=metadata,
            seen_at=str(row["seen_at"] or utc_now_iso()),
            received_at=str(row["received_at"] or row["seen_at"] or utc_now_iso()),
            acknowledged_at=str(row["acknowledged_at"] or ""),
            acknowledged_by=str(row["acknowledged_by"] or ""),
            acknowledgement_note=str(row["acknowledgement_note"] or ""),
        )

    def _row_from_report(self, report: IntelReport) -> tuple[Any, ...]:
        return (
            report.report_id,
            report.system,
            json.dumps(report.names, ensure_ascii=False),
            report.source,
            report.source_instance,
            report.system_id,
            json.dumps(report.character_ids, ensure_ascii=False),
            report.confidence,
            report.note,
            report.raw_text,
            json.dumps(report.metadata, ensure_ascii=False),
            report.seen_at,
            report.received_at,
            report.acknowledged_at,
            report.acknowledged_by,
            report.acknowledgement_note,
        )

    def _active_item_from_row(self, row: sqlite3.Row) -> ActiveIntelItem | None:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
            source_ids = json.loads(str(row["source_observation_ids_json"] or "[]"))
            if not isinstance(metadata, dict):
                return None
            if not isinstance(source_ids, list):
                return None
            active = self._strict_int(row["active"])
            if active not in {0, 1}:
                return None
            seen_count = self._strict_int(row["seen_count"])
            if seen_count <= 0:
                return None
            confidence: float | None
            if row["confidence"] in {None, ""}:
                confidence = None
            else:
                confidence = float(row["confidence"])
            active_id = str(row["active_id"] or "").strip()
            source = str(row["source"] or "").strip()
            system = self._normalize_system(str(row["system"] or ""))
            if not active_id or not source or not system:
                return None
            return ActiveIntelItem(
                active_id=active_id,
                source=source,
                source_instance=str(row["source_instance"] or ""),
                system_name=system,
                system_id=self._optional_int(row["system_id"]),
                target_type=str(row["target_type"] or "character"),
                name=str(row["name"] or ""),
                character_id=self._optional_int(row["character_id"]),
                raw_text=str(row["raw_text"] or ""),
                metadata=self._normalize_metadata(metadata),
                first_seen_at=str(row["first_seen_at"] or ""),
                last_seen_at=str(row["last_seen_at"] or ""),
                expires_at=str(row["expires_at"] or ""),
                left_at=str(row["left_at"] or ""),
                cleared_at=str(row["cleared_at"] or ""),
                active=bool(active),
                seen_count=seen_count,
                confidence=confidence,
                source_observation_ids=[str(value) for value in source_ids if value],
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def _strict_int(self, value: Any) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("+", "-")):
                sign = text[0]
                digits = text[1:]
            else:
                sign = ""
                digits = text
            if digits and digits.isdecimal():
                return int(f"{sign}{digits}")
        raise ValueError("value is not a strict integer")

    def _active_row(self, item: ActiveIntelItem) -> tuple[Any, ...]:
        return (
            item.active_id,
            item.source,
            item.source_instance,
            item.system_name,
            item.system_id,
            item.target_type,
            item.name,
            item.character_id,
            item.raw_text,
            json.dumps(item.metadata, ensure_ascii=False),
            item.first_seen_at,
            item.last_seen_at,
            item.expires_at,
            item.left_at,
            item.cleared_at,
            1 if item.active else 0,
            item.seen_count,
            item.confidence,
            json.dumps(item.source_observation_ids, ensure_ascii=False),
        )

    def _heartbeat_from_row(self, row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            details = json.loads(str(row["details_json"] or "{}"))
        except json.JSONDecodeError:
            return None
        if not isinstance(details, dict):
            return None
        client_id = str(row["client_id"] or "").strip()
        if not client_id:
            return None
        return {
            "client_id": client_id,
            "client_type": str(row["client_type"] or "client"),
            "label": str(row["label"] or client_id),
            "status": str(row["status"] or "running"),
            "seen_at": str(row["seen_at"] or utc_now_iso()),
            "heartbeat_interval_seconds": float(
                row["heartbeat_interval_seconds"] or 0
            ),
            "details": details,
        }

    def _heartbeat_row(self, heartbeat: dict[str, Any]) -> tuple[Any, ...]:
        return (
            heartbeat["client_id"],
            heartbeat["client_type"],
            heartbeat["label"],
            heartbeat["status"],
            heartbeat["seen_at"],
            heartbeat["heartbeat_interval_seconds"],
            json.dumps(heartbeat.get("details") or {}, ensure_ascii=False),
        )
