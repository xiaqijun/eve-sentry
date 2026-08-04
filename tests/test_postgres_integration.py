"""Integration coverage for PostgreSQL schema and retention behavior."""

from __future__ import annotations

import json
import os
import uuid

import pytest

from app.server.postgres_store import PostgreSQLIntelStore


POSTGRES_DSN = os.environ.get("EVE_SENTRY_TEST_POSTGRES_DSN", "").strip()
if not POSTGRES_DSN:
    pytest.skip(
        "EVE_SENTRY_TEST_POSTGRES_DSN is not configured",
        allow_module_level=True,
    )

psycopg = pytest.importorskip("psycopg")
sql = pytest.importorskip("psycopg.sql")
make_conninfo = pytest.importorskip("psycopg.conninfo").make_conninfo


def test_postgres_schema_persistence_and_retention_contract() -> None:
    suffix = uuid.uuid4().hex
    schema_name = f"eve_sentry_ci_{suffix}"
    isolated_dsn = make_conninfo(
        POSTGRES_DSN,
        options=f"-csearch_path={schema_name}",
    )
    active_ids = [
        f"ci-active-{suffix}",
        f"ci-inactive-old-{suffix}",
        f"ci-inactive-recent-{suffix}",
    ]
    store: PostgreSQLIntelStore | None = None

    with psycopg.connect(POSTGRES_DSN) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )

    try:
        store = PostgreSQLIntelStore(isolated_dsn, systems={}, links=[])
        active_report = store.add_report(
            "Tama",
            [f"Active Pilot {suffix}"],
            source="ci-integration",
            seen_at="2026-01-01T00:00:00+00:00",
        )
        expired_report = store.add_report(
            "Tama",
            [f"Expired Pilot {suffix}"],
            source="ci-integration",
            seen_at="2026-01-01T00:00:00+00:00",
        )
        active_report_id = active_report.report_id
        expired_report_id = expired_report.report_id

        with store._connect() as connection:
            connection.execute(
                """
                UPDATE intel_reports
                SET seen_at = ?, received_at = ?
                WHERE report_id IN (?, ?)
                """,
                (
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    active_report_id,
                    expired_report_id,
                ),
            )
            connection.executemany(
                """
                INSERT INTO active_intel (
                    active_id, source, source_instance, system, target_type,
                    name, raw_text, first_seen_at, last_seen_at, cleared_at,
                    active, source_observation_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        active_ids[0],
                        "ci-integration",
                        "ci",
                        "Tama",
                        "character",
                        f"Active Pilot {suffix}",
                        "",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        "",
                        1,
                        json.dumps([active_report_id]),
                    ),
                    (
                        active_ids[1],
                        "ci-integration",
                        "ci",
                        "Tama",
                        "character",
                        f"Inactive Old {suffix}",
                        "",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-02T00:00:00+00:00",
                        0,
                        "[]",
                    ),
                    (
                        active_ids[2],
                        "ci-integration",
                        "ci",
                        "Tama",
                        "character",
                        f"Inactive Recent {suffix}",
                        "",
                        "2026-07-29T00:00:00+00:00",
                        "2026-07-29T00:00:00+00:00",
                        "2026-07-29T01:00:00+00:00",
                        0,
                        "[]",
                    ),
                ],
            )

        reopened_store = PostgreSQLIntelStore(isolated_dsn, systems={}, links=[])
        store.close()
        store = reopened_store

        assert active_ids[0] in store._active_intel
        assert {active_report_id, expired_report_id}.issubset(
            {
                item["id"]
                for item in store.list_observations(source="ci-integration")
            }
        )
        assert store.prune_inactive_active_intel_older_than(
            30,
            now="2026-07-30T00:00:00+00:00",
        ) == 1

        with store._connect() as connection:
            remaining_active_rows = connection.execute(
                "SELECT active_id FROM active_intel WHERE active_id IN (?, ?, ?)",
                tuple(active_ids),
            ).fetchall()
            index_row = connection.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname = 'idx_active_intel_active_last_seen'
                """
            ).fetchone()

        assert {row["active_id"] for row in remaining_active_rows} == {
            active_ids[0],
            active_ids[2],
        }
        assert index_row is not None
        assert "WHERE (active = 1)" in str(index_row["indexdef"])
        assert store.delete_report(active_report_id) is False

        assert store.prune_reports_older_than(
            30,
            now="2026-07-30T00:00:00+00:00",
        ) == 1
        with store._connect() as connection:
            remaining_reports = connection.execute(
                "SELECT report_id FROM intel_reports WHERE report_id IN (?, ?)",
                (active_report_id, expired_report_id),
            ).fetchall()
        assert {row["report_id"] for row in remaining_reports} == {
            active_report_id
        }
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(POSTGRES_DSN) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


def test_postgres_heartbeat_attribution_migrates_and_roundtrips() -> None:
    suffix = uuid.uuid4().hex
    schema_name = f"eve_sentry_heartbeat_{suffix}"
    isolated_dsn = make_conninfo(
        POSTGRES_DSN,
        options=f"-csearch_path={schema_name}",
    )
    client_id = f"ci-heartbeat-{suffix}"
    store: PostgreSQLIntelStore | None = None

    with psycopg.connect(POSTGRES_DSN) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
        connection.execute(
            sql.SQL(
                """
                CREATE TABLE {}.client_heartbeats (
                    client_id TEXT PRIMARY KEY,
                    client_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    heartbeat_interval_seconds REAL NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{{}}'
                )
                """
            ).format(sql.Identifier(schema_name))
        )

    try:
        store = PostgreSQLIntelStore(isolated_dsn, systems={}, links=[])
        store.record_heartbeat(
            {
                "client_id": client_id,
                "client_type": "detector_client",
                "user_id": "ci-user",
                "api_key_id": "ci-key",
                "remote_ip": "203.0.113.40",
            }
        )
        store.close()
        store = PostgreSQLIntelStore(isolated_dsn, systems={}, links=[])

        managed = store.management_heartbeat_snapshot()["heartbeats"][0]
        assert managed["client_id"] == client_id
        assert managed["user_id"] == "ci-user"
        assert managed["api_key_id"] == "ci-key"
        assert managed["remote_ip"] == "203.0.113.40"

        with store._connect() as connection:
            columns = connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'client_heartbeats'
                """
            ).fetchall()
        assert {row["column_name"] for row in columns}.issuperset(
            {"user_id", "api_key_id", "remote_ip"}
        )
    finally:
        if store is not None:
            store.close()
        with psycopg.connect(POSTGRES_DSN) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
