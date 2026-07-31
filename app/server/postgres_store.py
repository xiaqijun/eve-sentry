"""PostgreSQL-backed hostile intel store."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.active_intel import (
    ActiveIntelItem,
    ActiveIntelSnapshotResult,
    DEFAULT_OCR_GRACE_SECONDS,
)
from app.core.models import Observation
from app.server.auth_store import migrate_auth_schema
from app.server.intel_store import (
    IntelReport,
    IntelStore,
    STALE_HEARTBEAT_STARTUP_GRACE_SECONDS,
    StarSystem,
    _OcrEsiTask,
    utc_now_iso,
)


POSTGRES_POOL_MIN_SIZE = 2
POSTGRES_POOL_MAX_SIZE = 8
POSTGRES_POOL_TIMEOUT_SECONDS = 5.0
DEFAULT_HOT_REPORT_LIMIT = 5000
POSTGRES_ID_LOOKUP_BATCH_SIZE = 1000


class PostgreSQLIntelStore(IntelStore):
    """Persist intel in PostgreSQL with a bounded in-memory hot set."""

    def __init__(
        self,
        dsn: str,
        import_json_path: str | Path | None = None,
        systems: dict[str, StarSystem] | None = None,
        links: list[tuple[str, str]] | None = None,
        resolver: Any | None = None,
        scorer: Any | None = None,
        enricher: Any | None = None,
        allow_unmapped_systems: bool = True,
        hot_report_limit: int = DEFAULT_HOT_REPORT_LIMIT,
    ) -> None:
        self._postgres_dsn = str(dsn or "").strip()
        if not self._postgres_dsn:
            raise ValueError("postgres dsn is required")
        self._postgres_safe_dsn = _redact_dsn(self._postgres_dsn)
        self._postgres_pool = _create_connection_pool(self._postgres_dsn)
        self._import_json_path = Path(import_json_path) if import_json_path else None
        self._hot_report_limit = max(1, int(hot_report_limit))
        self._startup_active_intel: dict[str, ActiveIntelItem] = {}
        try:
            super().__init__(
                filepath="postgresql",
                systems=systems,
                links=links,
                resolver=resolver,
                scorer=scorer,
                enricher=enricher,
                allow_unmapped_systems=allow_unmapped_systems,
            )
        except Exception:
            self._postgres_pool.close()
            raise
        self._active_intel = self._startup_active_intel
        del self._startup_active_intel
        self._heartbeats = self._read_heartbeats()
        if self._heartbeats:
            self._stale_heartbeat_cleanup_after = (
                time.monotonic() + STALE_HEARTBEAT_STARTUP_GRACE_SECONDS
            )

    def close(self, *, wait: bool = True) -> None:
        """Stop background work and close reusable PostgreSQL connections."""
        try:
            super().close(wait=wait)
        finally:
            self._postgres_pool.close()

    def _load_reports(self) -> list[IntelReport]:
        self._migrate()
        self._startup_active_intel = self._read_active_intel()
        if not self._has_reports() and self._import_json_path is not None:
            if (
                self._meta_value("legacy_json_imported") != "1"
                and self._import_json_path.exists()
            ):
                legacy = IntelStore(
                    self._import_json_path,
                    systems={},
                    links=[],
                    resolver=self._resolver,
                )._reports_snapshot()
                if legacy:
                    self._replace_reports(legacy)
                self._set_meta("legacy_json_imported", "1")
        referenced_ids = {
            report_id
            for item in self._startup_active_intel.values()
            for report_id in item.source_observation_ids
            if report_id
        }
        return self._read_hot_reports(referenced_ids)

    def _save_reports(self) -> None:
        for report in self._reports:
            self._upsert_report(report)

    def list_reports(
        self,
        system: str | None = None,
        name: str | None = None,
        limit: int | None = None,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        """Query PostgreSQL history without expanding the startup hot set."""
        report_items = self._visible_reports(
            self._read_reports(system=system),
            include_suppressed=include_suppressed,
        )
        return self._filter_report_like(
            [report.to_dict() for report in report_items],
            system=system,
            name=name,
            limit=limit,
        )

    def list_observations(
        self,
        source: str | None = None,
        system: str | None = None,
        name: str | None = None,
        limit: int | None = None,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        """Query PostgreSQL observations without expanding the startup hot set."""
        report_items = self._visible_reports(
            self._read_reports(source=source, system=system),
            include_suppressed=include_suppressed,
        )
        return self._filter_report_like(
            [report.to_observation().to_dict() for report in report_items],
            system=system,
            name=name,
            limit=limit,
            system_key="system_name",
        )

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
            active_before = self._active_rows_snapshot()
            duplicate = self._find_duplicate_observation(report)
            if duplicate is not None:
                self._apply_channel_active_state(duplicate)
                self._persist_active_intel_changes(active_before)
                return duplicate.to_observation()
            self._ensure_system(report.system)
            self._reports.append(report)
            self._apply_channel_active_state(report)
            self._upsert_report(report)
            self._persist_active_intel_changes(active_before)
        return report.to_observation()

    def record_ocr_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record an OCR snapshot and persist derived active intel state."""
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
        seen_at = self._clean_snapshot_seen_at(payload.get("seen_at"))
        hostile_icon_count = max(
            0,
            self._optional_int(payload.get("hostile_icon_count")) or 0,
        )
        defer_esi = self._resolver is not None or self._enricher is not None
        names = self._normalize_ocr_names(
            payload.get("names"),
            resolve=not defer_esi,
        )
        snapshot_metadata = (
            {
                "hostile_icon_detected": hostile_icon_count > 0,
                "hostile_icon_count": hostile_icon_count,
                "hostile_icon_seen_at": seen_at,
            }
            if "hostile_icon_count" in payload or not names
            else {}
        )
        raw_text = ", ".join(names)
        result = ActiveIntelSnapshotResult()
        seen_name_keys = {name.casefold() for name in names}
        esi_tasks: list[_OcrEsiTask] = []

        with self._lock:
            new_reports: list[IntelReport] = []
            changed_active_ids: set[str] = set()
            accepted, moved_items = self._transition_ocr_client_system(
                client_id,
                system_name,
                seen_at,
            )
            if not accepted:
                return result.to_dict(include_active=False)
            for item in moved_items:
                changed_active_ids.add(item.active_id)
            result.expired += len(moved_items)

            for name in names:
                active_id = self._active_ocr_id(client_id, system_name, name)
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
                        new_reports.append(report)
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
                            changed_active_ids.add(active_id)
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
                    changed_active_ids.add(active_id)
                    result.created += 1
                    continue

                elapsed = self._seconds_between_iso(item.last_seen_at, seen_at)
                if elapsed is None or elapsed >= 0:
                    item.last_seen_at = seen_at
                    item.source_instance = source_instance
                    item.raw_text = raw_text
                item.active = True
                item.left_at = ""
                for report in self._apply_hostile_icon_metadata(
                    item,
                    snapshot_metadata,
                ):
                    if report not in new_reports:
                        new_reports.append(report)
                changed_active_ids.add(active_id)
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

                for report in self._apply_hostile_icon_metadata(
                    item,
                    snapshot_metadata,
                ):
                    if report not in new_reports:
                        new_reports.append(report)
                changed_active_ids.add(item.active_id)

                elapsed = self._seconds_between_iso(item.last_seen_at, seen_at)
                if elapsed is None or elapsed <= DEFAULT_OCR_GRACE_SECONDS:
                    result.missing += 1
                    continue

                item.active = False
                item.left_at = seen_at
                changed_active_ids.add(item.active_id)
                result.expired += 1

            result.active = [
                item.to_dict()
                for item in self._active_intel.values()
                if item.active and item.source == source
            ]
            report_rows = [self._row_from_report(report) for report in new_reports]
            active_rows = [
                self._active_row(self._active_intel[active_id])
                for active_id in sorted(changed_active_ids)
                if active_id in self._active_intel
            ]
            with self._connect() as connection:
                if report_rows:
                    connection.executemany(
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
                        report_rows,
                    )
                self._upsert_active_intel_rows(connection, active_rows)
        for task in esi_tasks:
            self._esi_worker.submit(task.active_id, task)
        return result.to_dict(include_active=False)

    def _persist_ocr_esi_result(
        self,
        report: IntelReport,
        item: ActiveIntelItem | None,
        *,
        previous_active_id: str,
    ) -> None:
        self._upsert_report(report)
        with self._connect() as connection:
            if item is not None and previous_active_id != item.active_id:
                connection.execute(
                    "DELETE FROM active_intel WHERE active_id = ?",
                    (previous_active_id,),
                )
            if item is not None:
                self._upsert_active_intel_rows(connection, [self._active_row(item)])

    def expire_active_intel(self, now: str | None = None) -> int:
        """Expire TTL-based active intel and persist changed rows."""
        with self._lock:
            active_before = {
                active_id
                for active_id, item in self._active_intel.items()
                if item.active
            }
            expired = super().expire_active_intel(now)
            changed_rows = [
                self._active_row(item)
                for active_id, item in self._active_intel.items()
                if active_id in active_before and not item.active
            ]
            if changed_rows:
                with self._connect() as connection:
                    self._upsert_active_intel_rows(connection, changed_rows)
            return expired

    def alert_detail(self, alert_id: str) -> dict[str, Any] | None:
        """Return hot or historical alert details from PostgreSQL."""
        hot_detail = super().alert_detail(alert_id)
        if hot_detail is not None:
            return hot_detail
        report = self._report_for_alert_id(str(alert_id or "").strip())
        if report is None:
            return None
        alert = self._alert_from_report(report)
        if alert is None:
            return None
        alert_data = self._alert_to_dict(report, alert)
        if not self._alert_matches(alert_id, report, alert_data):
            return None
        observation = report.to_observation()
        context = self._alert_context(observation)
        degraded_sources = self._alert_degraded_sources(observation, context)
        return {
            "schema_version": "alert_detail.v1",
            "alert": alert_data,
            "observation": observation.to_dict(),
            "entities": self._alert_entities(observation, context),
            "context": context,
            "explanation": self._alert_explanation(
                alert_data,
                observation,
                context,
                degraded_sources=degraded_sources,
            ),
        }

    def alert_cursor(self, alert_id: str) -> str:
        """Return the cursor for a hot or historical PostgreSQL alert."""
        hot_cursor = super().alert_cursor(alert_id)
        if hot_cursor:
            return hot_cursor
        report = self._report_for_alert_id(str(alert_id or "").strip())
        if report is None:
            return ""
        alert = self._alert_from_report(report)
        if alert is None:
            return ""
        alert_data = self._alert_to_dict(report, alert)
        if not self._alert_matches(alert_id, report, alert_data):
            return ""
        return str(alert_data.get("created_at") or "")

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
                matched_report = self._report_for_alert_id(alert_id)

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
            stored_report = self._read_report_by_id(report_id)
            if stored_report is None:
                return False
            if not self._delete_report(report_id):
                return False
            self._reports = [
                report for report in self._reports if report.report_id != report_id
            ]
            self._alert_cache.pop(report_id, None)
            return True

    def prune_reports_older_than(
        self,
        retention_days: int,
        *,
        now: str | None = None,
    ) -> int:
        """Delete expired PostgreSQL rows without loading report history."""
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise ValueError("retention_days must be an integer")
        if retention_days < 0:
            raise ValueError("retention_days must not be negative")
        if retention_days == 0:
            return 0
        now_at = self._parse_timestamp(now or utc_now_iso())
        if now_at is None:
            raise ValueError("now must be an ISO timestamp")
        cutoff = (now_at - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            result = connection.execute(
                """
                WITH active_report_refs AS (
                    SELECT DISTINCT jsonb_array_elements_text(
                        COALESCE(
                            NULLIF(source_observation_ids_json, ''), '[]'
                        )::jsonb
                    ) AS report_id
                    FROM active_intel
                    WHERE active = 1
                )
                DELETE FROM intel_reports AS report
                WHERE COALESCE(
                    NULLIF(report.received_at, ''), report.seen_at
                )::timestamptz < ?::timestamptz
                  AND NOT EXISTS (
                      SELECT 1
                      FROM active_report_refs AS active_ref
                      WHERE active_ref.report_id = report.report_id
                  )
                """,
                (cutoff,),
            )
            removed_count = max(0, int(result.rowcount))
        if removed_count == 0:
            return 0
        with self._lock:
            cached_ids = [report.report_id for report in self._reports]
        existing_ids = self._read_existing_report_ids(cached_ids)
        with self._lock:
            self._reports = [
                report
                for report in self._reports
                if report.report_id in existing_ids
            ]
            for report_id in set(cached_ids) - existing_ids:
                self._alert_cache.pop(report_id, None)
        return removed_count

    def prune_inactive_active_intel_older_than(
        self,
        retention_days: int,
        *,
        now: str | None = None,
    ) -> int:
        """Delete inactive intel rows whose lifecycle ended before the cutoff."""
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise ValueError("retention_days must be an integer")
        if retention_days < 0:
            raise ValueError("retention_days must not be negative")
        if retention_days == 0:
            return 0
        now_at = self._parse_timestamp(now or utc_now_iso())
        if now_at is None:
            raise ValueError("now must be an ISO timestamp")
        cutoff = (now_at - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            result = connection.execute(
                """
                DELETE FROM active_intel
                WHERE active = 0
                  AND COALESCE(
                      NULLIF(cleared_at, ''),
                      NULLIF(left_at, ''),
                      NULLIF(last_seen_at, ''),
                      NULLIF(first_seen_at, '')
                  )::timestamptz < ?::timestamptz
                """,
                (cutoff,),
            )
            removed_count = max(0, int(result.rowcount))
        if removed_count == 0:
            return 0
        with self._lock:
            cached_inactive_ids = [
                active_id
                for active_id, item in self._active_intel.items()
                if not item.active
            ]
        existing_ids = self._read_existing_active_intel_ids(cached_inactive_ids)
        with self._lock:
            for active_id in set(cached_inactive_ids) - existing_ids:
                item = self._active_intel.get(active_id)
                if item is not None and not item.active:
                    self._active_intel.pop(active_id, None)
        return removed_count

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intel_reports (
                    report_id TEXT PRIMARY KEY,
                    system TEXT NOT NULL,
                    names_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_instance TEXT NOT NULL,
                    system_id BIGINT,
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
                CREATE INDEX IF NOT EXISTS idx_intel_reports_history_page
                ON intel_reports(seen_at DESC, received_at DESC, report_id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intel_reports_system_history_page
                ON intel_reports(
                    LOWER(system), seen_at DESC, received_at DESC, report_id DESC
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intel_reports_source_history_page
                ON intel_reports(
                    LOWER(source), seen_at DESC, received_at DESC, report_id DESC
                )
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
                    system_id BIGINT,
                    target_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    character_id BIGINT,
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
                "BIGINT",
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
                "BIGINT",
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
                CREATE INDEX IF NOT EXISTS idx_active_intel_active_last_seen
                ON active_intel(last_seen_at)
                WHERE active = 1
                """
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
            migrate_auth_schema(connection)

    def _has_reports(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM intel_reports LIMIT 1"
            ).fetchone()
        return row is not None

    def _read_hot_reports(
        self,
        referenced_ids: set[str],
    ) -> list[IntelReport]:
        with self._connect() as connection:
            recent_rows = connection.execute(
                """
                SELECT report_id, system, names_json, source, source_instance,
                       system_id, character_ids_json, confidence, note, raw_text,
                       metadata_json, seen_at, received_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note
                FROM intel_reports
                ORDER BY seen_at DESC, received_at DESC, report_id DESC
                LIMIT ?
                """,
                (self._hot_report_limit,),
            ).fetchall()
            referenced_rows: list[Any] = []
            if referenced_ids:
                ordered_ids = sorted(referenced_ids)
                placeholders = ", ".join("?" for _ in ordered_ids)
                referenced_rows = list(
                    connection.execute(
                        f"""
                        SELECT report_id, system, names_json, source, source_instance,
                               system_id, character_ids_json, confidence, note, raw_text,
                               metadata_json, seen_at, received_at, acknowledged_at,
                               acknowledged_by, acknowledgement_note
                        FROM intel_reports
                        WHERE report_id IN ({placeholders})
                        """,
                        tuple(ordered_ids),
                    ).fetchall()
                )

        reports_by_id: dict[str, IntelReport] = {}
        for row in [*recent_rows, *referenced_rows]:
            report = self._report_from_row(row)
            if report is not None:
                reports_by_id[report.report_id] = report
                self._ensure_system(report.system)
        return sorted(
            reports_by_id.values(),
            key=lambda report: (
                report.seen_at,
                report.received_at,
                report.report_id,
            ),
        )

    def _read_reports(
        self,
        *,
        source: str | None = None,
        system: str | None = None,
    ) -> list[IntelReport]:
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("LOWER(source) = ?")
            params.append(source.strip().casefold())
        if system:
            clauses.append("LOWER(system) = ?")
            params.append(system.strip().casefold())
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT report_id, system, names_json, source, source_instance,
                       system_id, character_ids_json, confidence, note, raw_text,
                       metadata_json, seen_at, received_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note
                FROM intel_reports
                {where_clause}
                ORDER BY seen_at DESC, received_at DESC, report_id DESC
                """,
                tuple(params),
            ).fetchall()
        reports: list[IntelReport] = []
        for row in rows:
            report = self._report_from_row(row)
            if report is not None:
                reports.append(report)
        return reports

    def _read_report_by_id(self, report_id: str) -> IntelReport | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT report_id, system, names_json, source, source_instance,
                       system_id, character_ids_json, confidence, note, raw_text,
                       metadata_json, seen_at, received_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note
                FROM intel_reports
                WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
        return self._report_from_row(row) if row is not None else None

    def _read_existing_report_ids(self, report_ids: list[str]) -> set[str]:
        if not report_ids:
            return set()
        existing_ids: set[str] = set()
        for offset in range(0, len(report_ids), POSTGRES_ID_LOOKUP_BATCH_SIZE):
            batch = report_ids[offset : offset + POSTGRES_ID_LOOKUP_BATCH_SIZE]
            placeholders = ", ".join("?" for _ in batch)
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT report_id
                    FROM intel_reports
                    WHERE report_id IN ({placeholders})
                    """,
                    tuple(batch),
                ).fetchall()
            existing_ids.update(str(row["report_id"]) for row in rows)
        return existing_ids

    def _read_existing_active_intel_ids(
        self,
        active_ids: list[str],
    ) -> set[str]:
        if not active_ids:
            return set()
        existing_ids: set[str] = set()
        for offset in range(0, len(active_ids), POSTGRES_ID_LOOKUP_BATCH_SIZE):
            batch = active_ids[offset : offset + POSTGRES_ID_LOOKUP_BATCH_SIZE]
            placeholders = ", ".join("?" for _ in batch)
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT active_id
                    FROM active_intel
                    WHERE active_id IN ({placeholders})
                    """,
                    tuple(batch),
                ).fetchall()
            existing_ids.update(str(row["active_id"]) for row in rows)
        return existing_ids

    def _report_for_alert_id(self, alert_id: str) -> IntelReport | None:
        report_id = alert_id[4:] if alert_id.startswith("evt_") else alert_id
        return self._read_report_by_id(report_id)

    def _report_page_items(
        self,
        *,
        cursor: str,
        limit: int,
        source: str | None = None,
        system: str | None = None,
        name: str | None = None,
        include_suppressed: bool = False,
    ) -> tuple[list[IntelReport], str]:
        limit = self._validate_page_limit(limit)
        scan_anchor = self._decode_report_page_cursor(cursor)
        batch_limit = max(100, min(1000, (limit + 1) * 2))
        collected: list[IntelReport] = []

        while len(collected) <= limit:
            rows = self._read_report_page_rows(
                anchor=scan_anchor,
                limit=batch_limit,
                source=source,
                system=system,
            )
            if not rows:
                break

            for row in rows:
                report = self._report_from_row(row)
                if report is None:
                    continue
                if (
                    not include_suppressed
                    and self._report_has_whitelisted_names(report)
                ):
                    continue
                if not self._report_matches_page_filters(
                    report,
                    source=source,
                    system=system,
                    name=name,
                ):
                    continue
                collected.append(report)
                if len(collected) > limit:
                    break

            last_row = rows[-1]
            scan_anchor = (
                str(last_row["seen_at"] or ""),
                str(last_row["received_at"] or ""),
                str(last_row["report_id"] or ""),
            )
            if len(collected) > limit or len(rows) < batch_limit:
                break

        page = collected[:limit]
        next_cursor = (
            self._encode_report_page_cursor(page[-1])
            if len(collected) > limit and page
            else ""
        )
        return page, next_cursor

    def _read_report_page_rows(
        self,
        *,
        anchor: tuple[str, str, str] | None,
        limit: int,
        source: str | None,
        system: str | None,
    ) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        source_query = str(source or "").strip().casefold()
        system_query = str(system or "").strip().casefold()
        if source_query:
            clauses.append("LOWER(source) = ?")
            params.append(source_query)
        if system_query:
            clauses.append("LOWER(system) = ?")
            params.append(system_query)
        if anchor is not None:
            seen_at, received_at, report_id = anchor
            clauses.append(
                "("
                "seen_at < ? "
                "OR (seen_at = ? AND received_at < ?) "
                "OR (seen_at = ? AND received_at = ? AND report_id < ?)"
                ")"
            )
            params.extend(
                [seen_at, seen_at, received_at, seen_at, received_at, report_id]
            )
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT report_id, system, names_json, source, source_instance,
                       system_id, character_ids_json, confidence, note, raw_text,
                       metadata_json, seen_at, received_at, acknowledged_at,
                       acknowledged_by, acknowledgement_note
                FROM intel_reports
                {where_clause}
                ORDER BY seen_at DESC, received_at DESC, report_id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return list(rows)

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
                WHERE active = 1
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

    def _active_rows_snapshot(self) -> dict[str, tuple[Any, ...]]:
        return {
            active_id: self._active_row(item)
            for active_id, item in self._active_intel.items()
        }

    def _persist_active_intel_changes(
        self,
        before: dict[str, tuple[Any, ...]],
    ) -> None:
        rows = [
            row
            for active_id, item in self._active_intel.items()
            if (row := self._active_row(item)) != before.get(active_id)
        ]
        if not rows:
            return
        with self._connect() as connection:
            self._upsert_active_intel_rows(connection, rows)

    def _upsert_active_intel_rows(
        self,
        connection: Any,
        rows: list[tuple[Any, ...]],
    ) -> None:
        if not rows:
            return
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
            ON CONFLICT(active_id) DO UPDATE SET
                source = excluded.source,
                source_instance = excluded.source_instance,
                system = excluded.system,
                system_id = excluded.system_id,
                target_type = excluded.target_type,
                name = excluded.name,
                character_id = excluded.character_id,
                raw_text = excluded.raw_text,
                metadata_json = excluded.metadata_json,
                first_seen_at = excluded.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                expires_at = excluded.expires_at,
                left_at = excluded.left_at,
                cleared_at = excluded.cleared_at,
                active = excluded.active,
                seen_count = excluded.seen_count,
                confidence = excluded.confidence,
                source_observation_ids_json = excluded.source_observation_ids_json
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

    def _delete_report(self, report_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                DELETE FROM intel_reports AS report
                WHERE report.report_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM active_intel AS intel
                      WHERE intel.active = 1
                        AND jsonb_exists(
                            COALESCE(
                                NULLIF(intel.source_observation_ids_json, ''),
                                '[]'
                            )::jsonb,
                            report.report_id
                        )
                  )
                RETURNING report.report_id
                """,
                (report_id,),
            ).fetchone()
        return row is not None

    def _persist_pruned_reports(self, report_ids: list[str]) -> None:
        if not report_ids:
            return
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM intel_reports WHERE report_id = ?",
                [(report_id,) for report_id in report_ids],
            )

    def record_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist heartbeats while keeping the base in-memory cache."""
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

    def _connect(self) -> "_PostgresConnection":
        return _PostgresConnection(self._postgres_pool.connection())

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
        connection: "_PostgresConnection",
        table: str,
        column: str,
        definition: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name = %s
            """,
            (table, column),
        ).fetchone()
        if row is not None:
            return
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _report_from_row(self, row: Any) -> IntelReport | None:
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

    def _active_item_from_row(self, row: Any) -> ActiveIntelItem | None:
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

    def _heartbeat_from_row(self, row: Any) -> dict[str, Any] | None:
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


class _PostgresConnection:
    """Return pooled PostgreSQL connections through a small query adapter."""

    def __init__(self, connection_context: Any) -> None:
        self._connection_context = connection_context
        self._connection: Any | None = None

    def __enter__(self) -> "_PostgresConnection":
        self._connection = self._connection_context.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(
                self._connection_context.__exit__(exc_type, exc_value, traceback)
            )
        finally:
            self._connection = None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> Any:
        if self._connection is None:
            raise RuntimeError("PostgreSQL connection is not active")
        return self._connection.execute(_convert_placeholders(query), params)

    def executemany(self, query: str, params_seq: list[tuple[Any, ...]]) -> None:
        if self._connection is None:
            raise RuntimeError("PostgreSQL connection is not active")
        with self._connection.cursor() as cursor:
            cursor.executemany(_convert_placeholders(query), params_seq)


def _create_connection_pool(dsn: str) -> Any:
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL storage requires psycopg with pool support"
        ) from exc
    return ConnectionPool(
        conninfo=dsn,
        min_size=POSTGRES_POOL_MIN_SIZE,
        max_size=POSTGRES_POOL_MAX_SIZE,
        timeout=POSTGRES_POOL_TIMEOUT_SECONDS,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def _convert_placeholders(query: str) -> str:
    return query.replace("?", "%s")


def _redact_dsn(dsn: str) -> str:
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "postgresql://[redacted]"
    if not parts.scheme:
        return "[redacted]"
    if "@" not in parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, "", ""))
