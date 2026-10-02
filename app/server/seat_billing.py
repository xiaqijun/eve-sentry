"""SeAT-compatible alert interval evidence persistence.

This module stores the Sentry-side facts needed by GloryNavy_Seat. Protocol v2
grants only freeze seconds, deliveries reserve an exact eligible warning
interval, and ACKs confirm that interval. Pricing, coin calculation, exchange
settlement and refunds remain Seat responsibilities. Protocol v1 rows remain
readable for historical reconciliation.
"""

from __future__ import annotations

import hashlib
import json
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
SEAT_TIME_GRANT_PROTOCOL_VERSION = 2
ALERT_ACK_PROTOCOL = "alert-ack.v1"
ALERT_GRANT_STATUSES = {"active", "revoked", "expired"}
ALERT_CONSUMPTION_STATES = {"reserved", "consumed", "released", "refunded"}
ALERT_DELIVERY_STATUSES = {"sent", "confirmed", "expired"}


class SeatBillingError(ValueError):
    """Stable API error for the Sentry/Seat consumption contract."""

    def __init__(self, message: str, code: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status = status


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_hash(payload: dict[str, Any]) -> str:
    """Hash the exact normalized JSON request used for idempotency checks."""
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def migrate_seat_billing_schema(connection: Any) -> None:
    """Create Seat alert ledgers and primary-monitor contribution evidence."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS seat_alert_grants (
            grant_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            account_id TEXT NOT NULL,
            key_id TEXT NOT NULL DEFAULT '',
            price_version TEXT NOT NULL,
            unit_seconds INTEGER NOT NULL CHECK (unit_seconds > 0),
            unit_price_minor INTEGER NOT NULL CHECK (unit_price_minor >= 0),
            reserved_seconds INTEGER NOT NULL CHECK (reserved_seconds > 0),
            remaining_seconds INTEGER NOT NULL CHECK (remaining_seconds >= 0),
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            protocol_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_seat_alert_grants_account ON seat_alert_grants(account_id, status, expires_at)",
        """
        CREATE TABLE IF NOT EXISTS seat_alert_events (
            charge_event_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            request_hash TEXT NOT NULL,
            wave_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            system_id BIGINT,
            system_name TEXT NOT NULL DEFAULT '',
            rule_version TEXT NOT NULL,
            eligibility_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            lifecycle TEXT NOT NULL DEFAULT 'eligible',
            revocation_reason TEXT NOT NULL DEFAULT '',
            protocol_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (charge_event_id, revision)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_seat_alert_events_created ON seat_alert_events(created_at, charge_event_id, revision)",
        """
        CREATE TABLE IF NOT EXISTS seat_alert_deliveries (
            delivery_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            charge_event_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            grant_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            client_version TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
            billed_units INTEGER NOT NULL CHECK (billed_units > 0),
            ack_deadline_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            sent_at TEXT NOT NULL,
            ack_at TEXT NOT NULL DEFAULT '',
            ack_idempotency_key TEXT NOT NULL DEFAULT '',
            ack_evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_seat_alert_deliveries_event ON seat_alert_deliveries(charge_event_id, revision, started_at, ended_at, created_at)",
        """
        CREATE TABLE IF NOT EXISTS seat_alert_consumptions (
            grant_id TEXT NOT NULL,
            charge_event_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
            billed_units INTEGER NOT NULL CHECK (billed_units > 0),
            delivery_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'reserved',
            reserved_at TEXT NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT '',
            released_at TEXT NOT NULL DEFAULT '',
            refunded_at TEXT NOT NULL DEFAULT '',
            refund_reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (grant_id, charge_event_id, revision, started_at, ended_at)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_seat_alert_consumptions_state ON seat_alert_consumptions(state, reserved_at)",
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
    )
    for statement in statements:
        connection.execute(statement)
    # Keep the embedded SQLite store compatible with databases created by the
    # first billing protocol revision.  The evidence is an append-only ACK
    # snapshot; it never changes the server-authoritative interval binding.
    # Keep the compatibility ALTER isolated.  PostgreSQL marks the whole
    # transaction as failed when ADD COLUMN sees an already-present column;
    # swallowing that exception without a savepoint would roll back every
    # table created above and make the next startup fail with missing tables.
    savepoint = "seat_billing_ack_evidence_column"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        connection.execute(
            "ALTER TABLE seat_alert_deliveries ADD COLUMN ack_evidence_json TEXT NOT NULL DEFAULT '{}'"
        )
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
    finally:
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")


class SeatBillingRepository:
    """SQL operations for alert billing and primary-monitor evidence."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def create_grant(self, record: dict[str, Any], operation_id: str, request_hash: str) -> dict[str, Any]:
        operation_id = str(operation_id or "").strip()
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM seat_alert_grants WHERE operation_id = ?", (operation_id,)).fetchone()
            if existing is not None:
                return {"grant": self._grant(dict(existing)), "created": False, "idempotency_conflict": str(existing["request_hash"]) != request_hash}
            connection.execute(
                """
                INSERT INTO seat_alert_grants (
                    grant_id, operation_id, request_hash, account_id, key_id,
                    price_version, unit_seconds, unit_price_minor, reserved_seconds,
                    remaining_seconds, expires_at, status, protocol_version,
                    created_at, updated_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record["grant_id"], operation_id, request_hash, record["account_id"], record.get("key_id", ""), record["price_version"], int(record["unit_seconds"]), int(record["unit_price_minor"]), int(record["reserved_seconds"]), int(record["reserved_seconds"]), record["expires_at"], "active", int(record.get("protocol_version") or SEAT_BILLING_PROTOCOL_VERSION), record["created_at"], record["created_at"], ""),
            )
            row = connection.execute("SELECT * FROM seat_alert_grants WHERE grant_id = ?", (record["grant_id"],)).fetchone()
        return {"grant": self._grant(dict(row)), "created": True, "idempotency_conflict": False}

    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM seat_alert_grants WHERE grant_id = ?", (str(grant_id),)).fetchone()
        return self._grant(dict(row)) if row is not None else None

    def revoke_grant(self, grant_id: str, reason: str, now: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM seat_alert_grants WHERE grant_id = ?", (str(grant_id),)).fetchone()
            if row is None:
                return None
            if str(row["status"]) == "active":
                connection.execute("UPDATE seat_alert_grants SET status = 'revoked', revoked_at = ?, updated_at = ? WHERE grant_id = ?", (str(now), str(now), str(grant_id)))
            updated = connection.execute("SELECT * FROM seat_alert_grants WHERE grant_id = ?", (str(grant_id),)).fetchone()
        return {**self._grant(dict(updated)), "revocation_reason": str(reason or "")}

    def upsert_event(self, record: dict[str, Any], request_hash: str) -> dict[str, Any]:
        event_id, revision = str(record["charge_event_id"]), int(record["revision"])
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM seat_alert_events WHERE charge_event_id = ? AND revision = ?", (event_id, revision)).fetchone()
            if existing is not None:
                return {"event": self._event(dict(existing)), "created": False, "idempotency_conflict": str(existing["request_hash"]) != request_hash}
            latest = connection.execute("SELECT MAX(revision) AS revision FROM seat_alert_events WHERE charge_event_id = ?", (event_id,)).fetchone()
            if latest is not None and latest["revision"] is not None and revision <= int(latest["revision"]):
                raise SeatBillingError("revision must increase for an existing charge event", "revision_conflict", HTTPStatus.CONFLICT)
            now = str(record.get("updated_at") or utc_now_iso())
            connection.execute(
                """
                INSERT INTO seat_alert_events (
                    charge_event_id, revision, request_hash, wave_id, event_type,
                    system_id, system_name, rule_version, eligibility_json,
                    evidence_json, lifecycle, revocation_reason, protocol_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, revision, request_hash, record.get("wave_id", ""), record["event_type"], record.get("system_id"), record.get("system_name", ""), record["rule_version"], json.dumps(record.get("eligibility") or {}, ensure_ascii=False, sort_keys=True), json.dumps(record.get("evidence") or {}, ensure_ascii=False, sort_keys=True), record.get("lifecycle", "eligible"), record.get("revocation_reason", ""), SEAT_BILLING_PROTOCOL_VERSION, str(record.get("created_at") or now), now),
            )
            row = connection.execute("SELECT * FROM seat_alert_events WHERE charge_event_id = ? AND revision = ?", (event_id, revision)).fetchone()
        return {"event": self._event(dict(row)), "created": True, "idempotency_conflict": False}

    def register_delivery(self, record: dict[str, Any], request_hash: str, now: str) -> dict[str, Any]:
        if str(record.get("ack_capability") or "").strip() != ALERT_ACK_PROTOCOL:
            raise SeatBillingError(
                "client does not advertise the alert ACK protocol",
                "client_ack_required",
                HTTPStatus.CONFLICT,
            )
        delivery_id, event_id, revision = str(record["delivery_id"]), str(record["charge_event_id"]), int(record["revision"])
        duration_seconds, started_at, ended_at = int(record["duration_seconds"]), str(record["started_at"]), str(record["ended_at"])
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM seat_alert_deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
            if existing is not None:
                return {"delivery": self._delivery(dict(existing)), "created": False, "idempotency_conflict": str(existing["request_hash"]) != request_hash}
            event = connection.execute("SELECT * FROM seat_alert_events WHERE charge_event_id = ? AND revision = ?", (event_id, revision)).fetchone()
            if event is None:
                raise SeatBillingError("charge event was not found", "charge_event_not_found", HTTPStatus.NOT_FOUND)
            if str(event["lifecycle"]) != "eligible":
                raise SeatBillingError("charge event is not eligible", "charge_event_not_eligible", HTTPStatus.CONFLICT)
            grant = connection.execute("SELECT * FROM seat_alert_grants WHERE grant_id = ?", (str(record["grant_id"]),)).fetchone()
            if grant is None:
                raise SeatBillingError("alert grant was not found", "grant_not_found", HTTPStatus.NOT_FOUND)
            if str(grant["account_id"]) != str(record["account_id"]):
                raise SeatBillingError("grant account does not match delivery", "grant_account_mismatch", HTTPStatus.FORBIDDEN)
            if str(grant["status"]) != "active" or str(grant["expires_at"]) <= str(now):
                raise SeatBillingError("alert grant is not active", "grant_expired", HTTPStatus.CONFLICT)
            billed_units = self._billed_units(duration_seconds, int(grant["unit_seconds"]))
            key = (str(record["grant_id"]), event_id, revision, started_at, ended_at)
            consumption = connection.execute("SELECT * FROM seat_alert_consumptions WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ?", key).fetchone()
            if consumption is None:
                overlap = connection.execute(
                    """
                    SELECT 1 FROM seat_alert_consumptions
                    WHERE grant_id = ? AND state IN ('reserved', 'consumed')
                      AND started_at < ? AND ended_at > ?
                    LIMIT 1
                    """,
                    (str(record["grant_id"]), ended_at, started_at),
                ).fetchone()
                if overlap is not None:
                    raise SeatBillingError("alert interval overlaps an existing consumption", "interval_overlap", HTTPStatus.CONFLICT)
                changed = connection.execute(
                    """
                    UPDATE seat_alert_grants
                    SET remaining_seconds = remaining_seconds - ?, updated_at = ?
                    WHERE grant_id = ? AND status = 'active' AND expires_at > ? AND remaining_seconds >= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM seat_alert_consumptions
                          WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ?
                      )
                    """,
                    (duration_seconds, str(now), str(record["grant_id"]), str(now), duration_seconds, *key),
                )
                if int(getattr(changed, "rowcount", 0)) == 1:
                    connection.execute(
                        """
                        INSERT INTO seat_alert_consumptions (
                            grant_id, charge_event_id, revision, started_at, ended_at,
                            duration_seconds, billed_units, delivery_id, account_id,
                            state, reserved_at, confirmed_at, released_at, refunded_at, refund_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, '', '', '', '')
                        """,
                        (*key, duration_seconds, billed_units, delivery_id, record["account_id"], str(now)),
                    )
                else:
                    consumption = connection.execute("SELECT * FROM seat_alert_consumptions WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ?", key).fetchone()
                    if consumption is None:
                        raise SeatBillingError("alert grant has no remaining seconds", "grant_seconds_exhausted", HTTPStatus.CONFLICT)
            connection.execute(
                """
                INSERT INTO seat_alert_deliveries (
                    delivery_id, request_hash, charge_event_id, revision, grant_id,
                    account_id, key_id, connection_id, client_version, started_at,
                    ended_at, duration_seconds, billed_units, ack_deadline_at, status,
                    sent_at, ack_at, ack_idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, '', '', ?, ?)
                """,
                (delivery_id, request_hash, event_id, revision, record["grant_id"], record["account_id"], record["key_id"], record["connection_id"], record.get("client_version", ""), started_at, ended_at, duration_seconds, billed_units, record["ack_deadline_at"], record.get("sent_at") or now, str(record.get("created_at") or now), str(record.get("created_at") or now)),
            )
            row = connection.execute("SELECT * FROM seat_alert_deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
        return {"delivery": self._delivery(dict(row)), "created": True, "idempotency_conflict": False}

    def acknowledge_delivery(self, delivery_id: str, *, account_id: str, key_id: str, charge_event_id: str, revision: int, started_at: str, ended_at: str, duration_seconds: int, connection_id: str, ack_idempotency_key: str, evidence: dict[str, Any] | None = None, now: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM seat_alert_deliveries WHERE delivery_id = ?", (str(delivery_id),)).fetchone()
            if row is None:
                raise SeatBillingError("delivery was not found", "delivery_not_found", HTTPStatus.NOT_FOUND)
            checks = {"account_id": account_id, "key_id": key_id, "charge_event_id": charge_event_id, "revision": int(revision), "started_at": started_at, "ended_at": ended_at, "duration_seconds": int(duration_seconds), "connection_id": connection_id}
            for field, expected in checks.items():
                if str(row[field]) != str(expected):
                    raise SeatBillingError("delivery binding does not match", "delivery_binding_mismatch", HTTPStatus.FORBIDDEN)
            if str(row["status"]) == "confirmed":
                return {"delivery": self._delivery(dict(row)), "idempotent_replay": True}
            if str(row["status"]) == "expired":
                raise SeatBillingError("delivery acknowledgement expired", "delivery_ack_expired", HTTPStatus.CONFLICT)
            consumption = connection.execute(
                """
                SELECT state FROM seat_alert_consumptions
                WHERE grant_id = ? AND charge_event_id = ? AND revision = ?
                  AND started_at = ? AND ended_at = ?
                """,
                (str(row["grant_id"]), str(charge_event_id), int(revision), started_at, ended_at),
            ).fetchone()
            if consumption is None or str(consumption["state"]) in {"released", "refunded"}:
                raise SeatBillingError("delivery acknowledgement expired", "delivery_ack_expired", HTTPStatus.CONFLICT)
            if not str(ack_idempotency_key or "").strip():
                raise SeatBillingError("ack_idempotency_key is required", "invalid_ack_idempotency_key")
            if not isinstance(evidence, dict):
                raise SeatBillingError("ack evidence must be an object", "invalid_ack_evidence")
            required_evidence = {
                "schema_version",
                "alert_id",
                "client_id",
                "connection_id",
                "received_at",
                "ui_handled_at",
                "dedupe",
                "ui_delivery",
            }
            if evidence.get("schema_version") != "alert-use-evidence.v1" or any(
                not str(evidence.get(field) or "").strip()
                for field in required_evidence - {"schema_version"}
            ):
                raise SeatBillingError(
                    "complete alert acknowledgement evidence is required",
                    "invalid_ack_evidence",
                )
            evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute("UPDATE seat_alert_deliveries SET status = 'confirmed', ack_at = ?, ack_idempotency_key = ?, ack_evidence_json = ?, updated_at = ? WHERE delivery_id = ? AND status = 'sent'", (str(now), str(ack_idempotency_key), evidence_json, str(now), str(delivery_id)))
            connection.execute("UPDATE seat_alert_consumptions SET state = 'consumed', confirmed_at = ? WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ? AND state = 'reserved'", (str(now), str(row["grant_id"]), str(charge_event_id), int(revision), started_at, ended_at))
            updated = connection.execute("SELECT * FROM seat_alert_deliveries WHERE delivery_id = ?", (str(delivery_id),)).fetchone()
        return {"delivery": self._delivery(dict(updated)), "idempotent_replay": False}

    def release_expired_consumptions(self, *, now: str) -> list[dict[str, Any]]:
        """Release unconfirmed interval reservations past their ACK deadline."""
        released: list[dict[str, Any]] = []
        with self._connect() as connection:
            rows = connection.execute("SELECT c.* FROM seat_alert_consumptions c JOIN seat_alert_deliveries d ON d.delivery_id = c.delivery_id WHERE c.state = 'reserved' AND d.status = 'sent' AND d.ack_deadline_at <= ?", (str(now),)).fetchall()
            for row in rows:
                changed = connection.execute("UPDATE seat_alert_consumptions SET state = 'released', released_at = ? WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ? AND state = 'reserved'", (str(now), row["grant_id"], row["charge_event_id"], int(row["revision"]), row["started_at"], row["ended_at"]))
                if int(getattr(changed, "rowcount", 0)) != 1:
                    continue
                connection.execute(
                    """
                    UPDATE seat_alert_grants
                    SET remaining_seconds = CASE
                        WHEN remaining_seconds + ? > reserved_seconds THEN reserved_seconds
                        ELSE remaining_seconds + ?
                    END,
                    updated_at = ?
                    WHERE grant_id = ?
                    """,
                    (int(row["duration_seconds"]), int(row["duration_seconds"]), str(now), row["grant_id"]),
                )
                connection.execute(
                    """
                    UPDATE seat_alert_deliveries
                    SET status = 'expired', updated_at = ?
                    WHERE grant_id = ? AND charge_event_id = ? AND revision = ?
                      AND started_at = ? AND ended_at = ? AND status = 'sent'
                    """,
                    (str(now), row["grant_id"], row["charge_event_id"], int(row["revision"]), row["started_at"], row["ended_at"]),
                )
                released.append(self._consumption(dict(row), state="released", released_at=str(now)))
        return released

    def refund_consumption(self, *, grant_id: str, charge_event_id: str, revision: int, started_at: str, ended_at: str, reason: str, now: str) -> dict[str, Any] | None:
        """Mark a confirmed interval as refunded for platform reconciliation."""
        key = (grant_id, charge_event_id, int(revision), started_at, ended_at)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM seat_alert_consumptions WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ?", key).fetchone()
            if row is None:
                return None
            if str(row["state"]) == "refunded":
                return self._consumption(dict(row))
            if str(row["state"]) != "consumed":
                raise SeatBillingError("only consumed intervals can be refunded", "consumption_not_consumed", HTTPStatus.CONFLICT)
            connection.execute("UPDATE seat_alert_consumptions SET state = 'refunded', refunded_at = ?, refund_reason = ? WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ? AND state = 'consumed'", (str(now), str(reason or ""), *key))
            updated = connection.execute("SELECT * FROM seat_alert_consumptions WHERE grant_id = ? AND charge_event_id = ? AND revision = ? AND started_at = ? AND ended_at = ?", key).fetchone()
        return self._consumption(dict(updated))

    def list_events(self, *, after: str = "", limit: int = 100) -> dict[str, Any]:
        page_limit = max(1, min(500, int(limit)))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM seat_alert_events WHERE (? = '' OR (created_at, charge_event_id, revision) > (?, ?, ?)) ORDER BY created_at ASC, charge_event_id ASC, revision ASC LIMIT ?", self._cursor_params(after, page_limit)).fetchall()
            earliest = connection.execute("SELECT MIN(created_at) AS created_at FROM seat_alert_events").fetchone()
            latest = connection.execute("SELECT created_at, charge_event_id, revision FROM seat_alert_events ORDER BY created_at DESC, charge_event_id DESC, revision DESC LIMIT 1").fetchone()
        events = [self._event(dict(row)) for row in rows]
        next_cursor = self._cursor(events[-1]["created_at"], events[-1]["charge_event_id"], events[-1]["revision"]) if events else ""
        watermark = self._cursor(str(latest["created_at"]), str(latest["charge_event_id"]), int(latest["revision"])) if latest is not None else ""
        return {"events": events, "next_cursor": next_cursor, "committed_watermark": watermark, "earliest_available_watermark": str(earliest["created_at"] or "") if earliest is not None else "", "has_more": len(events) >= page_limit, "protocol_version": SEAT_BILLING_PROTOCOL_VERSION}

    def list_deliveries(self, *, after: str = "", limit: int = 100) -> dict[str, Any]:
        """Return delivery/consumption facts for the Seat reconciliation worker."""
        page_limit = max(1, min(500, int(limit)))
        created, delivery_id = self._parse_delivery_cursor(after)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, COALESCE(c.state, '') AS consumption_state
                FROM seat_alert_deliveries d
                LEFT JOIN seat_alert_consumptions c
                  ON c.grant_id = d.grant_id
                 AND c.charge_event_id = d.charge_event_id
                 AND c.revision = d.revision
                 AND c.started_at = d.started_at
                 AND c.ended_at = d.ended_at
                WHERE (? = '' OR (d.created_at, d.delivery_id) > (?, ?))
                ORDER BY d.created_at ASC, d.delivery_id ASC
                LIMIT ?
                """,
                ("" if not after else after, created, delivery_id, page_limit),
            ).fetchall()
            earliest = connection.execute("SELECT MIN(created_at) AS created_at FROM seat_alert_deliveries").fetchone()
            latest = connection.execute("SELECT created_at, delivery_id FROM seat_alert_deliveries ORDER BY created_at DESC, delivery_id DESC LIMIT 1").fetchone()
        deliveries = [self._delivery(dict(row)) for row in rows]
        next_cursor = self._delivery_cursor(deliveries[-1]["created_at"], deliveries[-1]["delivery_id"]) if deliveries else ""
        watermark = self._delivery_cursor(str(latest["created_at"]), str(latest["delivery_id"])) if latest is not None else ""
        return {"deliveries": deliveries, "next_cursor": next_cursor, "committed_watermark": watermark, "earliest_available_watermark": str(earliest["created_at"] or "") if earliest is not None else "", "has_more": len(deliveries) >= page_limit, "protocol_version": SEAT_BILLING_PROTOCOL_VERSION}

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

    def _cursor_params(self, cursor: str, limit: int) -> tuple[Any, ...]:
        created, event_id, revision = self._parse_cursor(cursor)
        return ("" if not cursor else cursor, created, event_id, revision, limit)

    @staticmethod
    def _parse_cursor(cursor: str) -> tuple[str, str, int]:
        value = str(cursor or "")
        if not value:
            return "", "", 0
        parts = value.split("|", 2)
        if len(parts) != 3:
            raise SeatBillingError("cursor is invalid", "invalid_cursor")
        try:
            revision = int(parts[2])
        except ValueError as exc:
            raise SeatBillingError("cursor is invalid", "invalid_cursor") from exc
        return parts[0], parts[1], revision

    @staticmethod
    def _cursor(created_at: str, event_id: str, revision: int) -> str:
        return f"{created_at}|{event_id}|{int(revision)}"

    @staticmethod
    def _parse_delivery_cursor(cursor: str) -> tuple[str, str]:
        value = str(cursor or "")
        if not value:
            return "", ""
        parts = value.split("|", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise SeatBillingError("cursor is invalid", "invalid_cursor")
        return parts[0], parts[1]

    @staticmethod
    def _delivery_cursor(created_at: str, delivery_id: str) -> str:
        return f"{created_at}|{delivery_id}"

    @staticmethod
    def _parse_monitor_cursor(cursor: str) -> tuple[str, str]:
        value = str(cursor or "")
        if not value:
            return "", ""
        parts = value.split("|", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise SeatBillingError("cursor is invalid", "invalid_cursor")
        return parts[0], parts[1]

    @staticmethod
    def _monitor_cursor(created_at: str, contribution_id: str) -> str:
        return f"{created_at}|{contribution_id}"

    @staticmethod
    def _billed_units(duration_seconds: int, unit_seconds: int) -> int:
        if duration_seconds <= 0 or unit_seconds <= 0:
            raise SeatBillingError("interval duration and unit must be positive", "invalid_interval")
        return (int(duration_seconds) + int(unit_seconds) - 1) // int(unit_seconds)

    @staticmethod
    def _grant(row: dict[str, Any]) -> dict[str, Any]:
        return {"grant_id": str(row["grant_id"]), "operation_id": str(row["operation_id"]), "account_id": str(row["account_id"]), "key_id": str(row.get("key_id") or ""), "price_version": str(row["price_version"]), "unit_seconds": int(row["unit_seconds"]), "unit_price_minor": int(row["unit_price_minor"]), "reserved_seconds": int(row["reserved_seconds"]), "remaining_seconds": int(row["remaining_seconds"]), "expires_at": str(row["expires_at"]), "status": str(row["status"]), "protocol_version": int(row["protocol_version"]), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]), "revoked_at": str(row.get("revoked_at") or "")}

    @staticmethod
    def _event(row: dict[str, Any]) -> dict[str, Any]:
        def load(name: str) -> dict[str, Any]:
            try:
                value = json.loads(row.get(name) or "{}")
            except (TypeError, json.JSONDecodeError):
                value = {}
            return value if isinstance(value, dict) else {}
        return {"charge_event_id": str(row["charge_event_id"]), "wave_id": str(row.get("wave_id") or ""), "revision": int(row["revision"]), "event_type": str(row["event_type"]), "system_id": row.get("system_id"), "system_name": str(row.get("system_name") or ""), "rule_version": str(row["rule_version"]), "eligibility": load("eligibility_json"), "evidence": load("evidence_json"), "lifecycle": str(row["lifecycle"]), "revocation_reason": str(row.get("revocation_reason") or ""), "protocol_version": int(row["protocol_version"]), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}

    @staticmethod
    def _delivery(row: dict[str, Any]) -> dict[str, Any]:
        try:
            ack_evidence = json.loads(row.get("ack_evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            ack_evidence = {}
        if not isinstance(ack_evidence, dict):
            ack_evidence = {}
        return {"delivery_id": str(row["delivery_id"]), "charge_event_id": str(row["charge_event_id"]), "revision": int(row["revision"]), "grant_id": str(row["grant_id"]), "account_id": str(row["account_id"]), "key_id": str(row["key_id"]), "connection_id": str(row["connection_id"]), "client_version": str(row.get("client_version") or ""), "started_at": str(row["started_at"]), "ended_at": str(row["ended_at"]), "duration_seconds": int(row["duration_seconds"]), "billed_units": int(row["billed_units"]), "ack_deadline_at": str(row["ack_deadline_at"]), "status": str(row["status"]), "consumption_state": str(row.get("consumption_state") or ""), "sent_at": str(row["sent_at"]), "ack_at": str(row.get("ack_at") or ""), "ack_idempotency_key": str(row.get("ack_idempotency_key") or ""), "ack_evidence": ack_evidence, "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}

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
    def _consumption(row: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        result = {"grant_id": str(row["grant_id"]), "charge_event_id": str(row["charge_event_id"]), "revision": int(row["revision"]), "started_at": str(row["started_at"]), "ended_at": str(row["ended_at"]), "duration_seconds": int(row["duration_seconds"]), "billed_units": int(row["billed_units"]), "delivery_id": str(row["delivery_id"]), "account_id": str(row["account_id"]), "state": str(row["state"]), "reserved_at": str(row["reserved_at"]), "confirmed_at": str(row.get("confirmed_at") or ""), "released_at": str(row.get("released_at") or ""), "refunded_at": str(row.get("refunded_at") or ""), "refund_reason": str(row.get("refund_reason") or "")}
        result.update(overrides)
        return result
