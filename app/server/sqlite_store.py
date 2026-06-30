"""SQLite-backed hostile intel store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.server.intel_store import IntelReport, IntelStore, StarSystem, utc_now_iso


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
        )

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

    def _read_reports(self) -> list[IntelReport]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT report_id, system, names_json, source, source_instance,
                       system_id, character_ids_json, confidence, note, raw_text,
                       seen_at, received_at, acknowledged_at, acknowledged_by,
                       acknowledgement_note
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

    def _replace_reports(self, reports: list[IntelReport]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM intel_reports")
            connection.executemany(
                """
                INSERT INTO intel_reports (
                    report_id, system, names_json, source, source_instance,
                    system_id, character_ids_json, confidence, note, raw_text,
                    seen_at, received_at, acknowledged_at, acknowledged_by,
                    acknowledgement_note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._row_from_report(report) for report in reports],
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
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
            report.seen_at,
            report.received_at,
            report.acknowledged_at,
            report.acknowledged_by,
            report.acknowledgement_note,
        )
