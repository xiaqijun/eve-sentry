"""PostgreSQL-backed hostile intel store."""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from app.core.active_intel import (
    ActiveIntelItem,
    ActiveIntelSnapshotResult,
    DEFAULT_OCR_GRACE_SECONDS,
)
from app.core.models import Evidence, Observation, ThreatEvent
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
POSTGRES_ALERT_SCAN_BATCH_SIZE = 500
PERSISTED_ALERT_METADATA_KEY = "generated_alert"


logger = logging.getLogger(__name__)


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
        self._resume_pending_ocr_esi_tasks()

    def close(self, *, wait: bool = True) -> None:
        """Stop background work and close reusable PostgreSQL connections."""
        try:
            super().close(wait=wait)
        finally:
            self._postgres_pool.close()

    def _load_reports(self) -> list[IntelReport]:
        self._migrate()
        self._startup_active_intel = self._read_active_intel()
        self._reconcile_hostile_waves(self._startup_active_intel.values())
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
        if limit is not None:
            if limit <= 0:
                return []
            report_items, _ = self._report_page_items(
                cursor="",
                limit=limit,
                system=system,
                name=name,
                include_suppressed=include_suppressed,
            )
            return [report.to_dict() for report in report_items]
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
        if limit is not None:
            if limit <= 0:
                return []
            report_items, _ = self._report_page_items(
                cursor="",
                limit=limit,
                source=source,
                system=system,
                name=name,
                include_suppressed=include_suppressed,
            )
            return [report.to_observation().to_dict() for report in report_items]
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
            hostile_before = self._hostile_system_state()
            duplicate = self._find_duplicate_observation(report)
            if duplicate is not None:
                self._apply_channel_active_state(duplicate)
                hostile_waves = self._hostile_wave_changes(
                    hostile_before,
                    duplicate.seen_at or duplicate.received_at or utc_now_iso(),
                )
                with self._connect() as connection:
                    self._upsert_active_intel_rows(
                        connection,
                        self._changed_active_rows(active_before),
                    )
                    self._persist_hostile_wave_changes(connection, hostile_waves)
                return duplicate.to_observation()
            self._ensure_system(report.system)
            self._reports.append(report)
            self._apply_channel_active_state(report)
            self._upsert_report(report)
            hostile_waves = self._hostile_wave_changes(
                hostile_before,
                report.seen_at or report.received_at or utc_now_iso(),
            )
            with self._connect() as connection:
                self._upsert_active_intel_rows(
                    connection,
                    self._changed_active_rows(active_before),
                )
                self._persist_hostile_wave_changes(connection, hostile_waves)
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
            hostile_before = self._hostile_system_state()
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
                    for candidate in self._active_intel.values():
                        if (
                            candidate.active
                            and candidate.source == source
                            and candidate.target_type == "character"
                            and candidate.metadata.get("client_id") == client_id
                            and candidate.system_name.casefold()
                            == system_name.casefold()
                            and candidate.name.casefold() == name.casefold()
                        ):
                            active_id = candidate.active_id
                            item = candidate
                            break
                if item is None or not item.active:
                    active_id = self._active_ocr_reentry_id(
                        client_id,
                        system_name,
                        name,
                        seen_at,
                    )
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
                    identity_cached = not defer_esi
                    needs_esi_refresh = defer_esi
                    character_profiles: list[dict[str, Any]] = []
                    if defer_esi:
                        (
                            observation,
                            character_profiles,
                            identity_cached,
                            needs_esi_refresh,
                        ) = self._apply_cached_ocr_identity(observation)
                        report = self._report_from_observation(observation)
                    duplicate = self._find_duplicate_observation(report)
                    if duplicate is not None:
                        observation = duplicate.to_observation()
                    else:
                        self._ensure_system(report.system)
                        self._reports.append(report)
                        new_reports.append(report)
                    if not defer_esi:
                        character_profiles = self._character_profiles_for_observation(
                            observation
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
                    item_metadata = (
                        {
                            "client_id": client_id,
                            "identity_status": "pending",
                            **snapshot_metadata,
                        }
                        if defer_esi and not identity_cached
                        else self._active_ocr_metadata(
                            client_id,
                            observation,
                            checked_at=str(
                                observation.metadata.get("identity_checked_at") or ""
                            )
                            or None,
                            character_profiles=character_profiles,
                            cached_only=defer_esi and identity_cached,
                        )
                    )
                    if defer_esi and identity_cached:
                        item_metadata["identity_status"] = str(
                            observation.metadata.get("identity_status") or "unresolved"
                        )
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
                        name=(observation.names[0] if observation.names else name),
                        raw_text=raw_text,
                        metadata=item_metadata,
                        first_seen_at=seen_at,
                        last_seen_at=seen_at,
                        active=True,
                        seen_count=1,
                        source_observation_ids=[observation.observation_id],
                    )
                    if defer_esi and needs_esi_refresh:
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
                if item.target_type != "character":
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
            hostile_waves = self._hostile_wave_changes(hostile_before, seen_at)
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
                            metadata_json = (
                                COALESCE(NULLIF(excluded.metadata_json, ''), '{}')::jsonb
                                || jsonb_strip_nulls(jsonb_build_object(
                                    'generated_alert',
                                    COALESCE(
                                        NULLIF(intel_reports.metadata_json, ''),
                                        '{}'
                                    )::jsonb -> 'generated_alert'
                                ))
                            )::text,
                            seen_at = excluded.seen_at,
                            received_at = excluded.received_at,
                            acknowledged_at = excluded.acknowledged_at,
                            acknowledged_by = excluded.acknowledged_by,
                            acknowledgement_note = excluded.acknowledgement_note
                        """,
                        report_rows,
                    )
                self._upsert_active_intel_rows(connection, active_rows)
                self._persist_hostile_wave_changes(connection, hostile_waves)
        for task in esi_tasks:
            self._esi_worker.submit(task.active_id, task)
        return result.to_dict(include_active=False)

    def record_hostile_presence(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record and persist the latest detector visual-count state."""
        with self._lock:
            active_before = self._active_rows_snapshot()
            hostile_before = self._hostile_system_state()
            result = super().record_hostile_presence(payload)
            if not result.get("accepted", True):
                return result
            active_rows = self._changed_active_rows(active_before)
            hostile_waves = self._hostile_wave_changes(
                hostile_before,
                str(result.get("seen_at") or utc_now_iso()),
            )
            if active_rows or hostile_waves:
                with self._connect() as connection:
                    self._upsert_active_intel_rows(connection, active_rows)
                    self._persist_hostile_wave_changes(connection, hostile_waves)
            return result

    def _persist_ocr_esi_result(
        self,
        report: IntelReport,
        item: ActiveIntelItem | None,
        *,
        previous_active_id: str,
    ) -> None:
        self._upsert_report(report)
        with self._connect() as connection:
            system_key = str(
                item.system_name if item is not None else report.system
            ).strip().casefold()
            hostile_before = self._database_hostile_system_state(
                connection,
                system_key,
            )
            if item is not None and previous_active_id != item.active_id:
                connection.execute(
                    "DELETE FROM active_intel WHERE active_id = ?",
                    (previous_active_id,),
                )
            if item is not None:
                self._upsert_active_intel_rows(connection, [self._active_row(item)])
            hostile_after = {
                key: value
                for key, value in self._hostile_system_state().items()
                if key == system_key
            }
            self._persist_hostile_wave_changes(
                connection,
                self._hostile_wave_changes(
                    hostile_before,
                    str(
                        (item.last_seen_at if item is not None else report.seen_at)
                        or utc_now_iso()
                    ),
                    after=hostile_after,
                ),
            )

    def expire_active_intel(self, now: str | None = None) -> int:
        """Expire TTL-based active intel and persist changed rows."""
        with self._lock:
            hostile_before = self._hostile_system_state()
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
                    self._persist_hostile_wave_changes(
                        connection,
                        self._hostile_wave_changes(
                            hostile_before,
                            str(now or utc_now_iso()).strip() or utc_now_iso(),
                        ),
                    )
            return expired

    def list_hostile_waves(
        self,
        since: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query exact hostile-system wave lifecycles from PostgreSQL."""
        clean_since = str(since or "").strip()
        if clean_since and self._parse_timestamp(clean_since) is None:
            raise ValueError("since must be a valid ISO-8601 timestamp")
        if limit is not None and limit <= 0:
            return []

        clauses: list[str] = []
        params: list[Any] = []
        if clean_since:
            clauses.append(
                "COALESCE(NULLIF(cleared_at, ''), NULLIF(last_seen_at, ''), "
                "started_at)::timestamptz >= ?::timestamptz"
            )
            params.append(clean_since)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT wave_id, system_name, system_id, started_at,
                       last_seen_at, cleared_at, active, peak_hostile_count
                FROM hostile_waves
                {where_clause}
                ORDER BY started_at DESC, wave_id DESC
                {limit_clause}
                """,
                tuple(params),
            ).fetchall()
        return [self._hostile_wave_from_row(row) for row in rows]

    def _changed_active_rows(
        self,
        before: dict[str, tuple[Any, ...]],
    ) -> list[tuple[Any, ...]]:
        return [
            row
            for active_id, item in self._active_intel.items()
            if (row := self._active_row(item)) != before.get(active_id)
        ]

    def _hostile_system_state(
        self,
        items: Any | None = None,
    ) -> dict[str, dict[str, Any]]:
        source_items = self._active_intel.values() if items is None else items
        systems: dict[str, dict[str, Any]] = {}
        detector_counts: dict[str, dict[str, tuple[str, int]]] = {}
        for item in source_items:
            if not item.active:
                continue
            system_name = str(item.system_name or "").strip()
            if not system_name:
                continue
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            source = str(item.source or "").strip().casefold()
            detector_count: int | None = None
            if source == "eve-sentry-detector" and "hostile_icon_count" in metadata:
                try:
                    detector_count = max(
                        0,
                        int(metadata.get("hostile_icon_count") or 0),
                    )
                except (TypeError, ValueError):
                    detector_count = 0
            system_key = system_name.casefold()
            state = systems.get(system_key)
            first_seen_at = str(item.first_seen_at or item.last_seen_at or "").strip()
            last_seen_at = str(item.last_seen_at or item.first_seen_at or "").strip()
            if state is None:
                systems[system_key] = {
                    "system_key": system_key,
                    "system_name": system_name,
                    "system_id": item.system_id,
                    "first_seen_at": first_seen_at,
                    "last_seen_at": last_seen_at,
                    "hostile_count": 0,
                }
                state = systems[system_key]
            else:
                state["first_seen_at"] = self._earlier_iso(
                    str(state.get("first_seen_at") or ""),
                    first_seen_at,
                )
                state["last_seen_at"] = self._later_iso(
                    str(state.get("last_seen_at") or ""),
                    last_seen_at,
                )
                if state.get("system_id") is None and item.system_id is not None:
                    state["system_id"] = item.system_id

            if detector_count is not None:
                client_id = str(
                    metadata.get("client_id") or item.source_instance or "unknown"
                ).strip() or "unknown"
                snapshot_seen_at = str(
                    metadata.get("hostile_icon_seen_at") or last_seen_at
                ).strip()
                client_counts = detector_counts.setdefault(system_key, {})
                previous = client_counts.get(client_id)
                if previous is None or snapshot_seen_at >= previous[0]:
                    client_counts[client_id] = (snapshot_seen_at, detector_count)
                continue

            fallback_count = metadata.get("hostile_count")
            if isinstance(fallback_count, int) and fallback_count > 0:
                state["hostile_count"] += fallback_count
            else:
                state["hostile_count"] += 1

        for system_key, client_counts in detector_counts.items():
            if client_counts:
                systems[system_key]["hostile_count"] += max(
                    count for _, count in client_counts.values()
                )
        return {
            system_key: state
            for system_key, state in systems.items()
            if int(state.get("hostile_count") or 0) > 0
        }

    def _database_hostile_system_state(
        self,
        connection: Any,
        system_key: str,
    ) -> dict[str, dict[str, Any]]:
        if not system_key:
            return {}
        rows = connection.execute(
            """
            SELECT active_id, source, source_instance, system, system_id,
                   target_type, name, character_id, raw_text, metadata_json,
                   first_seen_at, last_seen_at, expires_at, left_at,
                   cleared_at, active, seen_count, confidence,
                   source_observation_ids_json
            FROM active_intel
            WHERE active = 1 AND LOWER(system) = ?
            """,
            (system_key,),
        ).fetchall()
        items = [
            item
            for row in rows
            if (item := self._active_item_from_row(row)) is not None
        ]
        return self._hostile_system_state(items)

    def _hostile_wave_changes(
        self,
        before: dict[str, dict[str, Any]],
        observed_at: str,
        *,
        after: dict[str, dict[str, Any]] | None = None,
        recover: bool = False,
    ) -> list[dict[str, Any]]:
        after_state = self._hostile_system_state() if after is None else after
        clean_observed_at = str(observed_at or utc_now_iso()).strip() or utc_now_iso()
        changes: list[dict[str, Any]] = []
        for system_key in sorted(set(before) | set(after_state)):
            previous = before.get(system_key)
            current = after_state.get(system_key)
            if current is not None:
                started_at = str(current.get("first_seen_at") or clean_observed_at)
                if previous is None and not recover:
                    started_at = clean_observed_at
                changes.append(
                    {
                        "action": "touch",
                        **current,
                        "started_at": started_at,
                        "last_seen_at": str(
                            current.get("last_seen_at") or clean_observed_at
                        ),
                    }
                )
                continue
            if previous is not None:
                changes.append(
                    {
                        "action": "clear",
                        **previous,
                        "started_at": str(
                            previous.get("first_seen_at") or clean_observed_at
                        ),
                        "last_seen_at": str(
                            previous.get("last_seen_at") or clean_observed_at
                        ),
                        "cleared_at": clean_observed_at,
                    }
                )
        return changes

    def _persist_hostile_wave_changes(
        self,
        connection: Any,
        changes: list[dict[str, Any]],
    ) -> None:
        for change in changes:
            if change["action"] == "touch":
                connection.execute(
                    """
                    INSERT INTO hostile_waves (
                        wave_id, system_key, system_name, system_id,
                        started_at, last_seen_at, cleared_at, active,
                        peak_hostile_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, '', 1, ?)
                    ON CONFLICT (system_key) WHERE active = 1 DO UPDATE SET
                        system_name = excluded.system_name,
                        system_id = COALESCE(excluded.system_id, hostile_waves.system_id),
                        peak_hostile_count = GREATEST(
                            hostile_waves.peak_hostile_count,
                            excluded.peak_hostile_count
                        ),
                        last_seen_at = CASE
                            WHEN hostile_waves.last_seen_at::timestamptz
                               >= excluded.last_seen_at::timestamptz
                            THEN hostile_waves.last_seen_at
                            ELSE excluded.last_seen_at
                        END
                    """,
                    (
                        uuid4().hex,
                        change["system_key"],
                        change["system_name"],
                        change.get("system_id"),
                        change["started_at"],
                        change["last_seen_at"],
                        max(0, int(change.get("hostile_count") or 0)),
                    ),
                )
                continue

            result = connection.execute(
                """
                UPDATE hostile_waves
                SET last_seen_at = CASE
                        WHEN last_seen_at::timestamptz >= ?::timestamptz
                        THEN last_seen_at
                        ELSE ?
                    END,
                    peak_hostile_count = GREATEST(peak_hostile_count, ?),
                    cleared_at = ?,
                    active = 0
                WHERE system_key = ? AND active = 1
                """,
                (
                    change["last_seen_at"],
                    change["last_seen_at"],
                    max(0, int(change.get("hostile_count") or 0)),
                    change["cleared_at"],
                    change["system_key"],
                ),
            )
            if max(0, int(result.rowcount)) > 0:
                continue
            connection.execute(
                """
                INSERT INTO hostile_waves (
                    wave_id, system_key, system_name, system_id,
                    started_at, last_seen_at, cleared_at, active,
                    peak_hostile_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    uuid4().hex,
                    change["system_key"],
                    change["system_name"],
                    change.get("system_id"),
                    change["started_at"],
                    change["last_seen_at"],
                    change["cleared_at"],
                    max(0, int(change.get("hostile_count") or 0)),
                ),
            )

    def _reconcile_hostile_waves(self, items: Any) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT system_key, system_name, system_id, started_at,
                       last_seen_at, peak_hostile_count
                FROM hostile_waves
                WHERE active = 1
                """
            ).fetchall()
            before = {
                str(row["system_key"]): {
                    "system_key": str(row["system_key"]),
                    "system_name": str(row["system_name"]),
                    "system_id": self._optional_int(row["system_id"]),
                    "first_seen_at": str(row["started_at"]),
                    "last_seen_at": str(row["last_seen_at"]),
                    "hostile_count": max(
                        0,
                        self._strict_int(row["peak_hostile_count"]),
                    ),
                }
                for row in rows
            }
            after = self._hostile_system_state(items)
            self._persist_hostile_wave_changes(
                connection,
                self._hostile_wave_changes(
                    before,
                    now,
                    after=after,
                    recover=True,
                ),
            )

    def _hostile_wave_from_row(self, row: Any) -> dict[str, Any]:
        try:
            peak_hostile_count = self._strict_int(row["peak_hostile_count"])
        except (KeyError, IndexError):
            peak_hostile_count = 0
        return {
            "id": str(row["wave_id"]),
            "system_name": str(row["system_name"]),
            "system_id": self._optional_int(row["system_id"]),
            "started_at": str(row["started_at"]),
            "last_seen_at": str(row["last_seen_at"]),
            "cleared_at": str(row["cleared_at"] or ""),
            "active": bool(self._strict_int(row["active"])),
            "peak_hostile_count": max(0, peak_hostile_count),
        }

    def _earlier_iso(self, left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        left_at = self._parse_timestamp(left)
        right_at = self._parse_timestamp(right)
        if left_at is None:
            return right
        if right_at is None:
            return left
        return left if left_at <= right_at else right

    def _later_iso(self, left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        left_at = self._parse_timestamp(left)
        right_at = self._parse_timestamp(right)
        if left_at is None:
            return right
        if right_at is None:
            return left
        return left if left_at >= right_at else right

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
                CREATE INDEX IF NOT EXISTS idx_intel_reports_alert_history
                ON intel_reports(received_at DESC, report_id DESC)
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
                CREATE INDEX IF NOT EXISTS idx_intel_reports_system_id
                ON intel_reports(system_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intel_reports_character_ids
                ON intel_reports USING GIN ((character_ids_json::jsonb))
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
                CREATE TABLE IF NOT EXISTS hostile_waves (
                    wave_id TEXT PRIMARY KEY,
                    system_key TEXT NOT NULL,
                    system_name TEXT NOT NULL,
                    system_id BIGINT,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    cleared_at TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    peak_hostile_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._ensure_column(
                connection,
                "hostile_waves",
                "peak_hostile_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_hostile_waves_open_system
                ON hostile_waves(system_key)
                WHERE active = 1
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hostile_waves_started_at
                ON hostile_waves(started_at DESC)
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
                    details_json TEXT NOT NULL DEFAULT '{}',
                    user_id TEXT NOT NULL DEFAULT '',
                    api_key_id TEXT NOT NULL DEFAULT '',
                    remote_ip TEXT NOT NULL DEFAULT ''
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
            self._ensure_column(
                connection,
                "client_heartbeats",
                "user_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "client_heartbeats",
                "api_key_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "client_heartbeats",
                "remote_ip",
                "TEXT NOT NULL DEFAULT ''",
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
        character_id: int | None = None,
        system_id: int | None = None,
    ) -> list[IntelReport]:
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("LOWER(source) = ?")
            params.append(source.strip().casefold())
        if system:
            clauses.append("LOWER(system) = ?")
            params.append(system.strip().casefold())
        if character_id is not None:
            clauses.append("character_ids_json::jsonb @> ?::jsonb")
            params.append(json.dumps([int(character_id)]))
        if system_id is not None:
            clauses.append("system_id = ?")
            params.append(int(system_id))
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

    def _reports_for_character_id(self, character_id: int) -> list[IntelReport]:
        return self._read_reports(character_id=character_id)

    def _reports_for_system_id(self, system_id: int) -> list[IntelReport]:
        return self._read_reports(system_id=system_id)

    def _alert_from_report(self, report: IntelReport) -> ThreatEvent | None:
        alert = super()._alert_from_report(report)
        if alert is not None:
            self._persist_generated_alert_snapshot(report, alert)
        return alert

    def _persist_generated_alert_snapshot(
        self,
        report: IntelReport,
        alert: ThreatEvent,
    ) -> None:
        snapshot = alert.to_dict()
        if report.metadata.get(PERSISTED_ALERT_METADATA_KEY) == snapshot:
            return
        metadata = dict(report.metadata)
        metadata[PERSISTED_ALERT_METADATA_KEY] = snapshot
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE intel_reports
                    SET metadata_json = (
                        COALESCE(NULLIF(metadata_json, ''), '{}')::jsonb
                        || jsonb_build_object(?::text, ?::jsonb)
                    )::text
                    WHERE report_id = ?
                    """,
                    (
                        PERSISTED_ALERT_METADATA_KEY,
                        json.dumps(snapshot, ensure_ascii=False),
                        report.report_id,
                    ),
                )
        except Exception as exc:
            logger.warning(
                "failed to persist alert snapshot for report %s: %s",
                report.report_id,
                exc,
            )
            return
        report.metadata = metadata

    def list_alert_history(
        self,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
        include_since: bool = False,
    ) -> list[dict[str, Any]]:
        """Query and rebuild historical alerts from bounded PostgreSQL pages."""
        since_query = str(since or "").strip()
        parsed_since = self._parse_timestamp(since_query) if since_query else None
        if since_query:
            if parsed_since is None:
                raise ValueError("since must be an ISO timestamp")
            since_query = parsed_since.astimezone(timezone.utc).isoformat()
        if limit is not None and limit <= 0:
            return []

        min_score_value = self._optional_score(min_score)
        min_level_rank = self._alert_level_rank(min_level)
        scan_anchor: tuple[str, str] | None = None
        alerts: list[dict[str, Any]] = []
        target_count = limit if limit is not None else None

        while target_count is None or len(alerts) < target_count:
            remaining = (
                POSTGRES_ALERT_SCAN_BATCH_SIZE
                if target_count is None
                else max(1, target_count - len(alerts))
            )
            batch_limit = max(
                100,
                min(POSTGRES_ALERT_SCAN_BATCH_SIZE, remaining * 2),
            )
            rows = self._read_alert_report_rows(
                anchor=scan_anchor,
                since=since_query,
                include_since=include_since,
                limit=batch_limit,
            )
            if not rows:
                break

            for row in rows:
                report = self._report_from_row(row)
                if report is None:
                    continue
                alert = self._alert_from_persisted_report(report)
                if alert is None:
                    continue
                alert_data = self._alert_to_dict(report, alert)
                if not self._alert_passes_filters(
                    alert_data,
                    acknowledged=acknowledged,
                    min_score=min_score_value,
                    min_level_rank=min_level_rank,
                ):
                    continue
                alerts.append(alert_data)
                if target_count is not None and len(alerts) >= target_count:
                    break

            last_row = rows[-1]
            scan_anchor = (
                str(last_row["received_at"] or ""),
                str(last_row["report_id"] or ""),
            )
            if len(rows) < batch_limit:
                break
        return alerts

    def _read_alert_report_rows(
        self,
        *,
        anchor: tuple[str, str] | None,
        since: str,
        include_since: bool,
        limit: int,
    ) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append(f"received_at {'>=' if include_since else '>'} ?")
            params.append(since)
        if anchor is not None:
            received_at, report_id = anchor
            clauses.append(
                "(received_at < ? OR (received_at = ? AND report_id < ?))"
            )
            params.extend([received_at, received_at, report_id])
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
                ORDER BY received_at DESC, report_id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return list(rows)

    def _alert_from_persisted_report(
        self,
        report: IntelReport,
    ) -> ThreatEvent | None:
        persisted_alert = self._threat_event_from_snapshot(report)
        if persisted_alert is not None:
            return persisted_alert

        classify = getattr(self._scorer, "classify", None)
        if not callable(classify):
            return (
                ThreatEvent.from_observation(report.to_observation())
                if self._scorer is None
                else None
            )

        character_profiles = self._persisted_character_profiles(report)
        observation = report.to_observation()
        names = self._normalize_names(observation.names)
        if not names and observation.character_ids:
            names = [str(item) for item in observation.character_ids]
        if not names and observation.raw_text:
            names = [observation.raw_text]
        try:
            result = classify(
                observation,
                names,
                character_profiles,
            )
        except Exception:
            result = None

        if result is None:
            alert = None
        else:
            classification = str(getattr(result, "classification", "")).strip()
            score = 100 if classification == "red" else 1
            alert = ThreatEvent(
                event_id=f"evt_{observation.observation_id}",
                system_name=observation.system_name,
                system_id=observation.system_id,
                names=names,
                character_ids=list(observation.character_ids),
                score=score,
                level="critical" if classification == "red" else "low",
                evidence=list(getattr(result, "evidence", None) or []),
                source_observation_id=observation.observation_id,
                created_at=observation.received_at,
                scoring_version=str(
                    getattr(self._scorer, "scoring_version", "") or ""
                ),
                classification=classification,
                reason=str(getattr(result, "reason", "") or ""),
            )
        return alert

    def _threat_event_from_snapshot(
        self,
        report: IntelReport,
    ) -> ThreatEvent | None:
        snapshot = report.metadata.get(PERSISTED_ALERT_METADATA_KEY)
        if not isinstance(snapshot, dict):
            return None
        source_id = str(snapshot.get("source_observation_id") or "").strip()
        if source_id and source_id != report.report_id:
            return None
        score = self._optional_score(snapshot.get("score"))
        if score is None:
            return None

        evidence: list[Evidence] = []
        raw_evidence = snapshot.get("evidence")
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if not isinstance(item, dict):
                    continue
                weight = self._optional_score(item.get("weight"))
                if weight is None:
                    continue
                evidence.append(
                    Evidence(
                        evidence_type=str(item.get("type") or "persisted_alert"),
                        weight=weight,
                        summary=str(item.get("summary") or ""),
                        rule_id=str(item.get("rule_id") or ""),
                    )
                )

        observation = report.to_observation()
        names = self._normalize_names(snapshot.get("names"))
        if not names:
            names = self._normalize_names(observation.names)
        character_ids = self._normalize_ints(snapshot.get("character_ids"))
        if not character_ids:
            character_ids = list(observation.character_ids)
        return ThreatEvent(
            event_id=str(snapshot.get("id") or f"evt_{report.report_id}"),
            system_name=str(
                snapshot.get("system_name")
                or snapshot.get("system")
                or observation.system_name
            ),
            system_id=self._optional_int(
                snapshot.get("system_id")
                if snapshot.get("system_id") is not None
                else observation.system_id
            ),
            names=names,
            character_ids=character_ids,
            score=score,
            level=str(snapshot.get("level") or ""),
            evidence=evidence,
            source_observation_id=source_id or report.report_id,
            created_at=str(
                snapshot.get("created_at")
                or snapshot.get("seen_at")
                or observation.received_at
            ),
            scoring_version=str(snapshot.get("scoring_version") or ""),
            classification=str(snapshot.get("classification") or ""),
            reason=str(snapshot.get("reason") or ""),
        )

    def _persisted_character_profiles(
        self,
        report: IntelReport,
    ) -> list[dict[str, Any]]:
        """Read historical profile inputs without triggering ESI enrichment."""
        profiles_by_id: dict[int, dict[str, Any]] = {}
        unkeyed_profiles: list[dict[str, Any]] = []
        resolver_cache = getattr(getattr(self, "_resolver", None), "cache", None)
        for character_id in self._normalize_ints(report.character_ids):
            disk_profile = None
            if resolver_cache is not None:
                read_cached = getattr(resolver_cache, "get", None)
                if callable(read_cached):
                    try:
                        disk_profile = read_cached(f"character:{character_id}")
                    except Exception:
                        disk_profile = None
                if not isinstance(disk_profile, dict):
                    read_stale = getattr(resolver_cache, "get_stale", None)
                    if callable(read_stale):
                        try:
                            disk_profile = read_stale(f"character:{character_id}")
                        except Exception:
                            disk_profile = None
            profile = dict(disk_profile) if isinstance(disk_profile, dict) else {}
            with self._lock:
                memory_profile = self._character_profile_cache.get(character_id)
            if isinstance(memory_profile, dict):
                profile.update(memory_profile)
            if profile:
                profile.setdefault("character_id", character_id)
                profiles_by_id[character_id] = profile

        metadata_profiles = report.metadata.get("character_profiles")
        if isinstance(metadata_profiles, list):
            for item in metadata_profiles:
                if not isinstance(item, dict):
                    continue
                metadata_profile = dict(item)
                character_id = self._optional_int(
                    metadata_profile.get("character_id")
                )
                if character_id is None:
                    unkeyed_profiles.append(metadata_profile)
                    continue
                profile = profiles_by_id.setdefault(character_id, {})
                profile.update(metadata_profile)
                profile.setdefault("character_id", character_id)

        return [*unkeyed_profiles, *profiles_by_id.values()]

    def alert_for_observation(self, observation_id: str) -> dict[str, Any] | None:
        report = self._read_report_by_id(str(observation_id or "").strip())
        if report is None:
            return None
        alert = self._alert_from_report(report)
        return self._alert_to_dict(report, alert) if alert is not None else None

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
                    metadata_json = (
                        COALESCE(NULLIF(excluded.metadata_json, ''), '{}')::jsonb
                        || jsonb_strip_nulls(jsonb_build_object(
                            'generated_alert',
                            COALESCE(
                                NULLIF(intel_reports.metadata_json, ''),
                                '{}'
                            )::jsonb -> 'generated_alert'
                        ))
                    )::text,
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
        with self._lock:
            raw = dict(self._heartbeats[heartbeat["client_id"]])
            raw["details"] = dict(raw.get("details") or {})
        self._write_heartbeat(raw)
        return heartbeat

    def refresh_detector_heartbeat(
        self,
        upload_client_id: str,
    ) -> dict[str, Any] | None:
        """Persist implicit detector activity from OCR or presence uploads."""
        heartbeat = super().refresh_detector_heartbeat(upload_client_id)
        if heartbeat is None:
            return None
        with self._lock:
            raw = dict(self._heartbeats[heartbeat["client_id"]])
            raw["details"] = dict(raw.get("details") or {})
        self._write_heartbeat(raw)
        return heartbeat

    def _read_heartbeats(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT client_id, client_type, label, status, seen_at,
                       heartbeat_interval_seconds, details_json,
                       user_id, api_key_id, remote_ip
                FROM client_heartbeats
                ORDER BY seen_at DESC
                """
            ).fetchall()

        heartbeats: dict[str, dict[str, Any]] = {}
        seen_logical_clients: set[tuple[str, str, str]] = set()
        duplicate_client_ids: list[str] = []
        for row in rows:
            heartbeat = self._heartbeat_from_row(row)
            if heartbeat is not None:
                details = heartbeat.get("details")
                details = details if isinstance(details, dict) else {}
                owner_id = str(heartbeat.get("user_id") or "").strip()
                client_type = str(
                    heartbeat.get("client_type") or "client"
                ).strip()
                host = str(details.get("host") or "").strip().casefold()
                if owner_id and host:
                    logical_key = (owner_id, client_type, host)
                    if logical_key in seen_logical_clients:
                        duplicate_client_ids.append(heartbeat["client_id"])
                        continue
                    seen_logical_clients.add(logical_key)
                heartbeats[heartbeat["client_id"]] = heartbeat

        if duplicate_client_ids:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM client_heartbeats WHERE client_id = ?",
                    [(client_id,) for client_id in duplicate_client_ids],
                )
            logger.info(
                "pruned %s duplicate client heartbeat records",
                len(duplicate_client_ids),
            )
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
                    details_json,
                    user_id,
                    api_key_id,
                    remote_ip
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_type = excluded.client_type,
                    label = excluded.label,
                    status = excluded.status,
                    seen_at = excluded.seen_at,
                    heartbeat_interval_seconds = excluded.heartbeat_interval_seconds,
                    details_json = excluded.details_json,
                    user_id = excluded.user_id,
                    api_key_id = excluded.api_key_id,
                    remote_ip = excluded.remote_ip
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
            "user_id": str(row.get("user_id") or "").strip(),
            "api_key_id": str(row.get("api_key_id") or "").strip(),
            "remote_ip": str(row.get("remote_ip") or "").strip(),
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
            str(heartbeat.get("user_id") or "").strip(),
            str(heartbeat.get("api_key_id") or "").strip(),
            str(heartbeat.get("remote_ip") or "").strip(),
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
