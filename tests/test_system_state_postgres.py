"""Projection consistency tests against an isolated real PostgreSQL schema."""

import json

import pytest

from app.server.postgres_store import PostgreSQLIntelStore, _PostgresConnection
from app.server.system_state import migrate_system_state
from tests.test_personnel_postgres import postgres_dsn  # noqa: F401

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row


@pytest.fixture
def connect(postgres_dsn):  # noqa: F811
    def connect():
        return _PostgresConnection(psycopg.connect(postgres_dsn, row_factory=dict_row))

    with connect() as connection:
        connection.execute("""
            CREATE TABLE intel_events (
                seq BIGSERIAL PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                event_type TEXT NOT NULL, entity_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        migrate_system_state(connection)
    return connect


def event(key, count, *, system="s-kswl"):
    return {
        "event_key": key,
        "event_type": "alert.updated" if count else "alert.cleared",
        "entity_key": system,
        "occurred_at": "2026-09-13T00:00:00+00:00",
        "payload": {
            "system_name": system.upper(),
            "hostile_count": count,
            "active": bool(count),
            "hostile_personnel": [],
        },
    }


def append(connection, *events):
    PostgreSQLIntelStore._persist_intel_events(None, connection, list(events))


def test_projection_and_event_commit_or_rollback_together(connect):
    with connect() as connection:
        append(connection, event("first", 1))
    with pytest.raises(RuntimeError), connect() as connection:
        append(connection, event("clear", 0))
        raise RuntimeError("simulate caller transaction failure")
    with connect() as connection:
        row = connection.execute("SELECT * FROM system_current_state").fetchone()
        events = connection.execute("SELECT * FROM intel_events").fetchall()
    assert len(events) == 1
    assert row["state_version"] == events[0]["seq"]
    assert json.loads(row["payload_json"])["hostile_count"] == 1


def test_duplicate_cannot_rewind_clear_or_inflate_version(connect):
    with connect() as connection:
        append(connection, event("first", 1), event("clear", 0), event("first", 1))
    with connect() as connection:
        row = connection.execute("SELECT * FROM system_current_state").fetchone()
        latest = connection.execute(
            "SELECT MAX(seq) AS seq FROM intel_events"
        ).fetchone()
    assert row["state_version"] == latest["seq"]
    assert json.loads(row["payload_json"])["hostile_count"] == 0


def test_backfill_is_idempotent_and_preserves_state_after_log_pruning(connect):
    with connect() as connection:
        append(connection, event("first", 1), event("other", 2, system="hb-fso"))
        connection.execute("DELETE FROM system_current_state")
        migrate_system_state(connection)
        before = connection.execute(
            "SELECT * FROM system_current_state ORDER BY system_key"
        ).fetchall()
        connection.execute("DELETE FROM intel_events WHERE entity_key = 's-kswl'")
        migrate_system_state(connection)
        after = connection.execute(
            "SELECT * FROM system_current_state ORDER BY system_key"
        ).fetchall()
    assert after == before
    assert len(after) == 2


def test_projection_failure_also_rolls_back_event(connect):
    with connect() as connection:
        connection.execute(
            "ALTER TABLE system_current_state ADD CHECK (system_key != 'reject')"
        )
    with pytest.raises(psycopg.errors.CheckViolation), connect() as connection:
        append(connection, event("invalid", 1, system="reject"))
    with connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) AS n FROM intel_events").fetchone()["n"]
            == 0
        )
