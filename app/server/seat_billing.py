"""SeAT-compatible online-time evidence persistence.

This module stores the Sentry-side facts needed by GloryNavy_Seat. Charging and
monitor rewards are based on effective online time intervals; Seat owns pricing
and coin settlement. Event, delivery, ACK and historical grant compatibility
are intentionally removed from the current contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

# v1 remains the event/delivery wire version. v2 is used only for the new
# time-grant request, which deliberately removes pricing from the Sentry
# contract: Seat owns policy, coin reservation, settlement and refunds;
# Sentry only authorizes/report seconds and delivery evidence.
SEAT_BILLING_PROTOCOL_VERSION = 1


class SeatBillingError(ValueError):
    """Stable API error for the Sentry/Seat consumption contract."""

    def __init__(self, message: str, code: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = status


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def migrate_seat_billing_schema(connection: Any) -> None:
    """Create the Seat-side online-time evidence and monitor export tables."""
    statements = (
        # The former event/grant/delivery billing model is intentionally
        # removed. Existing local databases are reset to the time-only schema.
        "DROP TABLE IF EXISTS seat_alert_deliveries",
        "DROP TABLE IF EXISTS seat_alert_consumptions",
        "DROP TABLE IF EXISTS seat_alert_events",
        "DROP TABLE IF EXISTS seat_alert_grants",
        """
        CREATE TABLE IF NOT EXISTS seat_monitor_contributions (
            contribution_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL DEFAULT '',
            key_id TEXT NOT NULL DEFAULT '',
            client_id TEXT NOT NULL,
            system_id BIGINT,
            system_name TEXT NOT NULL,
            primary_generation INTEGER NOT NULL CHECK (primary_generation > 0),
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
            rule_version TEXT NOT NULL DEFAULT 'primary-presence.v1',
            eligibility TEXT NOT NULL DEFAULT 'eligible',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE (client_id, system_name, primary_generation, started_at, ended_at)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_seat_monitor_contributions_created ON seat_monitor_contributions(created_at, contribution_id)",
        "CREATE INDEX IF NOT EXISTS idx_seat_monitor_contributions_system ON seat_monitor_contributions(system_name, started_at, ended_at)",
        """
        CREATE TABLE IF NOT EXISTS seat_client_usage (
            usage_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            system_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
            created_at TEXT NOT NULL,
            UNIQUE (client_id, system_id, started_at, ended_at)
        )
        """,
        # Older production installs created this table before system
        # attribution was added. Keep startup migration additive so existing
        # usage rows remain readable while new heartbeats can be persisted.
        (
            "ALTER TABLE seat_client_usage ADD COLUMN system_id TEXT NOT NULL DEFAULT ''"
            if isinstance(connection, sqlite3.Connection)
            else "ALTER TABLE seat_client_usage ADD COLUMN IF NOT EXISTS system_id TEXT NOT NULL DEFAULT ''"
        ),
        "CREATE INDEX IF NOT EXISTS idx_seat_client_usage_created ON seat_client_usage(created_at, usage_id)",
        """
        CREATE TABLE IF NOT EXISTS seat_integration_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            alert_consumption_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    for statement in statements:
        try:
            connection.execute(statement)
        except sqlite3.OperationalError as exc:
            # SQLite has no ADD COLUMN IF NOT EXISTS; an already-upgraded
            # local database is the one expected idempotent exception.
            if isinstance(connection, sqlite3.Connection) and "duplicate column" in str(exc).lower():
                continue
            raise

    # The original time-only table keyed a row by client and interval.  A
    # warning client can cover more than one monitored system during the same
    # heartbeat interval, so the system attribution is part of the identity.
    # PostgreSQL can replace the old generated UNIQUE constraint in place;
    # SQLite installs created before this change are rebuilt below because it
    # cannot drop an auto-index backing a table constraint.
    if isinstance(connection, sqlite3.Connection):
        indexes = connection.execute("PRAGMA index_list(seat_client_usage)").fetchall()
        legacy_index = False
        for index in indexes:
            if not int(index[2]):
                continue
            columns = [
                row[2]
                for row in connection.execute(f"PRAGMA index_info({index[1]!r})").fetchall()
            ]
            if columns == ["client_id", "started_at", "ended_at"]:
                legacy_index = True
                break
        if legacy_index:
            connection.execute("ALTER TABLE seat_client_usage RENAME TO seat_client_usage_legacy")
            connection.execute(
                """
                CREATE TABLE seat_client_usage (
                    usage_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    system_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                    created_at TEXT NOT NULL,
                    UNIQUE (client_id, system_id, started_at, ended_at)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO seat_client_usage
                    (usage_id, account_id, key_id, client_id, system_id,
                     started_at, ended_at, duration_seconds, created_at)
                SELECT usage_id, account_id, key_id, client_id,
                       coalesce(system_id, ''), started_at, ended_at,
                       duration_seconds, created_at
                FROM seat_client_usage_legacy
                """
            )
            connection.execute("DROP TABLE seat_client_usage_legacy")
    else:
        connection.execute(
            "ALTER TABLE seat_client_usage DROP CONSTRAINT IF EXISTS seat_client_usage_client_id_started_at_ended_at_key"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_seat_client_usage_identity ON seat_client_usage(client_id, system_id, started_at, ended_at)"
        )

class SeatBillingRepository:
    """SQL operations for online-time billing and primary-monitor evidence."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def alert_consumption_enabled(self, default: bool = False) -> bool:
        """Read the durable warning gate, seeding it from the deployment default once."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO seat_integration_settings
                    (id, alert_consumption_enabled, updated_at, updated_by)
                VALUES (1, ?, ?, 'environment')
                ON CONFLICT (id) DO NOTHING
                """,
                (bool(default), utc_now_iso()),
            )
            row = connection.execute(
                "SELECT alert_consumption_enabled FROM seat_integration_settings WHERE id = 1"
            ).fetchone()
        return bool(row["alert_consumption_enabled"]) if row is not None else bool(default)

    def set_alert_consumption_enabled(self, enabled: bool, *, updated_by: str = "seat") -> dict[str, Any]:
        """Persist the warning gate changed by the trusted Seat integration."""
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO seat_integration_settings
                    (id, alert_consumption_enabled, updated_at, updated_by)
                VALUES (1, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    alert_consumption_enabled = EXCLUDED.alert_consumption_enabled,
                    updated_at = EXCLUDED.updated_at,
                    updated_by = EXCLUDED.updated_by
                """,
                (bool(enabled), now, str(updated_by or "seat")),
            )
            row = connection.execute(
                "SELECT alert_consumption_enabled, updated_at, updated_by FROM seat_integration_settings WHERE id = 1"
            ).fetchone()
        return {
            "enabled": bool(row["alert_consumption_enabled"]) if row is not None else bool(enabled),
            "updated_at": str(row["updated_at"]) if row is not None else now,
            "updated_by": str(row["updated_by"] or "") if row is not None else str(updated_by or "seat"),
        }

    def record_monitor_contribution(
        self,
        record: dict[str, Any],
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        """Persist one server-confirmed primary-node monitoring interval."""
        client_id = str(record.get("client_id") or "").strip()
        system_name = str(record.get("system_name") or "").strip()
        started_at = str(record.get("started_at") or "").strip()
        ended_at = str(record.get("ended_at") or "").strip()
        try:
            generation = int(record.get("primary_generation") or 0)
            duration_seconds = int(record.get("duration_seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise SeatBillingError(
                "monitor contribution interval is invalid",
                "invalid_monitor_contribution",
            ) from exc
        if (
            not client_id
            or not system_name
            or not started_at
            or not ended_at
            or generation <= 0
            or duration_seconds <= 0
        ):
            raise SeatBillingError(
                "monitor contribution interval is invalid",
                "invalid_monitor_contribution",
            )
        contribution_id = str(record.get("contribution_id") or "").strip()
        if not contribution_id:
            contribution_id = hashlib.sha256(
                "|".join(
                    (
                        client_id,
                        system_name.casefold(),
                        str(generation),
                        started_at,
                        ended_at,
                    )
                ).encode("utf-8")
            ).hexdigest()
        created_at = str(record.get("created_at") or utc_now_iso())
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        normalized = {
            "contribution_id": contribution_id,
            "account_id": str(record.get("account_id") or ""),
            "key_id": str(record.get("key_id") or ""),
            "client_id": client_id,
            "system_id": record.get("system_id"),
            "system_name": system_name,
            "primary_generation": generation,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
            "rule_version": str(record.get("rule_version") or "primary-presence.v1"),
            "eligibility": str(record.get("eligibility") or "eligible"),
            "evidence_json": json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "created_at": created_at,
        }
        connection_context = nullcontext(connection) if connection is not None else self._connect()
        with connection_context as connection:
            existing = connection.execute(
                "SELECT * FROM seat_monitor_contributions WHERE contribution_id = ?",
                (contribution_id,),
            ).fetchone()
            if existing is not None:
                return {"contribution": self._monitor_contribution(dict(existing)), "created": False}
            connection.execute(
                """
                INSERT INTO seat_monitor_contributions (
                    contribution_id, account_id, key_id, client_id, system_id,
                    system_name, primary_generation, started_at, ended_at,
                    duration_seconds, rule_version, eligibility, evidence_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_id, system_name, primary_generation, started_at, ended_at)
                DO NOTHING
                """,
                tuple(normalized.values()),
            )
            row = connection.execute(
                "SELECT * FROM seat_monitor_contributions WHERE contribution_id = ?",
                (contribution_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM seat_monitor_contributions
                    WHERE client_id = ? AND system_name = ? AND primary_generation = ?
                      AND started_at = ? AND ended_at = ?
                    """,
                    (client_id, system_name, generation, started_at, ended_at),
                ).fetchone()
        return {
            "contribution": self._monitor_contribution(dict(row)) if row is not None else normalized,
            "created": row is not None and str(row["contribution_id"]) == contribution_id,
        }

    def list_monitor_contributions(self, *, after: str = "", limit: int = 100) -> dict[str, Any]:
        """Return primary-only monitoring evidence for Seat reconciliation."""
        page_limit = max(1, min(500, int(limit)))
        created_at, contribution_id = self._parse_monitor_cursor(after)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM seat_monitor_contributions
                WHERE (? = '' OR (created_at, contribution_id) > (?, ?))
                ORDER BY created_at ASC, contribution_id ASC
                LIMIT ?
                """,
                ("" if not after else after, created_at, contribution_id, page_limit),
            ).fetchall()
            earliest = connection.execute(
                "SELECT MIN(created_at) AS created_at FROM seat_monitor_contributions"
            ).fetchone()
            latest = connection.execute(
                "SELECT created_at, contribution_id FROM seat_monitor_contributions ORDER BY created_at DESC, contribution_id DESC LIMIT 1"
            ).fetchone()
        contributions = [self._monitor_contribution(dict(row)) for row in rows]
        next_cursor = (
            self._monitor_cursor(
                contributions[-1]["created_at"], contributions[-1]["contribution_id"]
            )
            if contributions
            else ""
        )
        watermark = (
            self._monitor_cursor(str(latest["created_at"]), str(latest["contribution_id"]))
            if latest is not None
            else ""
        )
        return {
            "contributions": contributions,
            "next_cursor": next_cursor,
            "committed_watermark": watermark,
            "earliest_available_watermark": str(earliest["created_at"] or "") if earliest is not None else "",
            "has_more": len(contributions) >= page_limit,
            "protocol_version": SEAT_BILLING_PROTOCOL_VERSION,
            "rule_version": "primary-presence.v1",
        }

    def record_client_usage(self, record: dict[str, Any]) -> dict[str, Any]:
        """Persist one authenticated client heartbeat interval for time billing."""
        account_id = str(record.get("account_id") or "").strip()
        key_id = str(record.get("key_id") or "").strip()
        client_id = str(record.get("client_id") or "").strip()
        started_at = str(record.get("started_at") or "").strip()
        ended_at = str(record.get("ended_at") or "").strip()
        try:
            duration_seconds = int(record.get("duration_seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise SeatBillingError("client usage duration is invalid", "invalid_client_usage") from exc
        if not account_id or not key_id or not client_id or not started_at or not ended_at or duration_seconds <= 0:
            raise SeatBillingError("client usage interval is invalid", "invalid_client_usage")
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SeatBillingError("client usage interval is invalid", "invalid_client_usage") from exc
        if ended <= started or int((ended - started).total_seconds()) != duration_seconds:
            raise SeatBillingError("client usage interval is invalid", "invalid_client_usage")
        usage_id = str(record.get("usage_id") or "").strip()
        if not usage_id:
            usage_id = hashlib.sha256(f"{client_id}|{started_at}|{ended_at}".encode()).hexdigest()
        created_at = str(record.get("created_at") or utc_now_iso())
        normalized = {
            "usage_id": usage_id,
            "account_id": account_id,
            "key_id": key_id,
            "client_id": client_id,
            "system_id": str(record.get("system_id") or "").strip(),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
            "created_at": created_at,
        }
        with self._connect() as connection:
            system_ids = []
            supplied_system_ids = record.get("system_ids")
            if isinstance(supplied_system_ids, (list, tuple, set)):
                system_ids = [str(value or "").strip() for value in supplied_system_ids]
            if not system_ids and normalized["system_id"]:
                system_ids = [normalized["system_id"]]
            if not system_ids:
                # A client heartbeat is not tied to one warning galaxy.  Use
                # the server-confirmed primary monitoring intervals that
                # overlap this heartbeat to expand one online interval into
                # one billable row per monitored system.
                rows = connection.execute(
                    """
                    SELECT DISTINCT system_id
                    FROM seat_monitor_contributions
                    WHERE account_id = ?
                      AND system_id IS NOT NULL
                      AND started_at < ?
                      AND ended_at > ?
                    ORDER BY system_id
                    """,
                    (account_id, ended_at, started_at),
                ).fetchall()
                system_ids = [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]
            if not system_ids:
                # Preserve the old evidence row when no monitored-system
                # evidence exists; the next reconciled interval can still be
                # attributed once the server has a primary snapshot.
                system_ids = [""]
            system_ids = list(dict.fromkeys(system_ids))
            saved: list[dict[str, Any]] = []
            created = False
            for system_id in system_ids:
                row_usage_id = usage_id if len(system_ids) == 1 else f"{usage_id}:{system_id}"
                row_data = dict(normalized)
                row_data["usage_id"] = row_usage_id
                row_data["system_id"] = system_id
                inserted = connection.execute(
                    """
                    INSERT INTO seat_client_usage (
                        usage_id, account_id, key_id, client_id, system_id,
                        started_at, ended_at, duration_seconds, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (client_id, system_id, started_at, ended_at) DO NOTHING
                    """,
                    tuple(row_data.values()),
                )
                created = created or int(getattr(inserted, "rowcount", 0)) == 1
                row = connection.execute("SELECT * FROM seat_client_usage WHERE usage_id = ?", (row_usage_id,)).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        SELECT * FROM seat_client_usage
                        WHERE client_id = ? AND system_id = ? AND started_at = ? AND ended_at = ?
                        """,
                        (client_id, system_id, started_at, ended_at),
                    ).fetchone()
                if row is not None:
                    saved.append(self._client_usage(dict(row)))
            first = saved[0] if saved else normalized
        return {
            "usage": first,
            "usages": saved,
            "created": created,
        }

    def list_client_usage(self, *, after: str = "", limit: int = 100) -> dict[str, Any]:
        """Return authenticated client online intervals in stable cursor order."""
        page_limit = max(1, min(500, int(limit)))
        created_at, usage_id = self._parse_monitor_cursor(after)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM seat_client_usage
                WHERE (? = '' OR (created_at, usage_id) > (?, ?))
                ORDER BY created_at ASC, usage_id ASC
                LIMIT ?
                """,
                ("" if not after else after, created_at, usage_id, page_limit),
            ).fetchall()
            earliest = connection.execute("SELECT MIN(created_at) AS created_at FROM seat_client_usage").fetchone()
            latest = connection.execute("SELECT created_at, usage_id FROM seat_client_usage ORDER BY created_at DESC, usage_id DESC LIMIT 1").fetchone()
        usage = [self._client_usage(dict(row)) for row in rows]
        next_cursor = self._monitor_cursor(usage[-1]["created_at"], usage[-1]["usage_id"]) if usage else ""
        watermark = self._monitor_cursor(str(latest["created_at"]), str(latest["usage_id"])) if latest is not None else ""
        return {
            "usage": usage,
            "next_cursor": next_cursor,
            "committed_watermark": watermark,
            "earliest_available_watermark": str(earliest["created_at"] or "") if earliest is not None else "",
            "has_more": len(usage) >= page_limit,
            "protocol_version": SEAT_BILLING_PROTOCOL_VERSION,
            "rule_version": "client-heartbeat.v1",
        }

    @staticmethod
    def _parse_monitor_cursor(cursor: str) -> tuple[str, str]:
        if not cursor:
            return "", ""
        try:
            created_at, identifier = str(cursor).split("|", 1)
        except ValueError as exc:
            raise SeatBillingError("cursor is invalid", "invalid_cursor") from exc
        if not created_at or not identifier:
            raise SeatBillingError("cursor is invalid", "invalid_cursor")
        return created_at, identifier

    @staticmethod
    def _monitor_cursor(created_at: str, identifier: str) -> str:
        return f"{created_at}|{identifier}"

    @staticmethod
    def _monitor_contribution(row: dict[str, Any]) -> dict[str, Any]:
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        return {
            "contribution_id": str(row["contribution_id"]),
            "account_id": str(row.get("account_id") or ""),
            "key_id": str(row.get("key_id") or ""),
            "client_id": str(row["client_id"]),
            "system_id": row.get("system_id"),
            "system_name": str(row["system_name"]),
            "primary_generation": int(row["primary_generation"]),
            "started_at": str(row["started_at"]),
            "ended_at": str(row["ended_at"]),
            "duration_seconds": int(row["duration_seconds"]),
            "rule_version": str(row.get("rule_version") or "primary-presence.v1"),
            "eligibility": str(row.get("eligibility") or "eligible"),
            "evidence": evidence,
            "created_at": str(row["created_at"]),
            "protocol_version": SEAT_BILLING_PROTOCOL_VERSION,
        }

    @staticmethod
    def _client_usage(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "usage_id": str(row["usage_id"]),
            "account_id": str(row["account_id"]),
            "key_id": str(row["key_id"]),
            "client_id": str(row["client_id"]),
            "system_id": str(row.get("system_id") or ""),
            "started_at": str(row["started_at"]),
            "ended_at": str(row["ended_at"]),
            "duration_seconds": int(row["duration_seconds"]),
            "created_at": str(row["created_at"]),
            "protocol_version": SEAT_BILLING_PROTOCOL_VERSION,
            "rule_version": "client-heartbeat.v1",
        }
