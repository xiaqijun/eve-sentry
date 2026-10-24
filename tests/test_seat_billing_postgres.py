"""Real PostgreSQL upgrade and online-time evidence regressions."""

from contextlib import contextmanager
import os
import uuid

import pytest

from app.server.postgres_store import _PostgresConnection
from app.server.seat_billing import SeatBillingRepository, migrate_seat_billing_schema


DSN = os.environ.get("EVE_SENTRY_TEST_POSTGRES_DSN", "").strip()
if not DSN:
    pytest.skip("EVE_SENTRY_TEST_POSTGRES_DSN is not configured", allow_module_level=True)

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.rows import dict_row


@pytest.fixture
def database():
    schema = "seat_billing_test_" + uuid.uuid4().hex
    with psycopg.connect(DSN) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    @contextmanager
    def connect():
        with _PostgresConnection(psycopg.connect(DSN, row_factory=dict_row)) as connection:
            connection.execute(f'SET LOCAL search_path TO "{schema}"')
            yield connection

    try:
        with connect() as connection:
            migrate_seat_billing_schema(connection)
        yield connect
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def interval(**overrides):
    return {
        "account_id": "account-a", "key_id": "key-a", "client_id": "shared-client",
        "system_id": "30000142", "system_name": "Jita", "primary_generation": 1,
        "started_at": "2026-10-24T12:00:00+00:00",
        "ended_at": "2026-10-24T12:00:10+00:00", "duration_seconds": 10,
        **overrides,
    }


@pytest.mark.parametrize("legacy_columns", [
    "client_id, started_at, ended_at",
    "client_id, system_id, started_at, ended_at",
])
@pytest.mark.parametrize("legacy_kind", ["index", "constraint"])
def test_upgrade_preserves_usage_and_separates_accounts_and_keys(database, legacy_columns, legacy_kind):
    repository = SeatBillingRepository(database)
    saved = repository.record_client_usage(interval())["usage"]
    with database() as connection:
        constraints = connection.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'seat_client_usage'::regclass AND contype = 'u'"
        ).fetchall()
        for row in constraints:
            connection.execute('ALTER TABLE seat_client_usage DROP CONSTRAINT "' + row["conname"] + '"')
        connection.execute("DROP INDEX IF EXISTS idx_seat_client_usage_identity")
        if legacy_kind == "index":
            connection.execute(f"CREATE UNIQUE INDEX idx_seat_client_usage_identity ON seat_client_usage ({legacy_columns})")
        else:
            connection.execute(f"ALTER TABLE seat_client_usage ADD UNIQUE ({legacy_columns})")
        connection.execute("CREATE INDEX unrelated_usage_account ON seat_client_usage(account_id)")

    with database() as connection:
        migrate_seat_billing_schema(connection)
        before = connection.execute("SELECT indexrelid FROM pg_index WHERE indrelid='seat_client_usage'::regclass ORDER BY indexrelid").fetchall()
        migrate_seat_billing_schema(connection)
        after = connection.execute("SELECT indexrelid FROM pg_index WHERE indrelid='seat_client_usage'::regclass ORDER BY indexrelid").fetchall()
        assert after == before

    assert repository.record_client_usage(interval())["created"] is False
    assert repository.record_client_usage(interval(account_id="account-b"))["created"] is True
    assert repository.record_client_usage(interval(key_id="key-b"))["created"] is True
    rows = repository.list_client_usage()["usage"]
    assert len(rows) == 3
    assert next(row for row in rows if row["usage_id"] == saved["usage_id"]) == saved
    with database() as connection:
        assert connection.execute("SELECT to_regclass('unrelated_usage_account') AS name").fetchone()["name"]


def test_upgrade_removes_truncated_monitor_constraint(database):
    repository = SeatBillingRepository(database)
    saved = repository.record_monitor_contribution(interval())["contribution"]
    with database() as connection:
        # PostgreSQL truncates the generated constraint name to 63 bytes.
        connection.execute("ALTER TABLE seat_monitor_contributions ADD UNIQUE (client_id, system_name, primary_generation, started_at, ended_at)")
        migrate_seat_billing_schema(connection)
        migrate_seat_billing_schema(connection)
    assert repository.record_monitor_contribution(interval())["created"] is False
    assert repository.record_monitor_contribution(interval(account_id="account-b"))["created"] is True
    assert repository.record_monitor_contribution(interval(key_id="key-b"))["created"] is True
    rows = repository.list_monitor_contributions()["contributions"]
    assert len(rows) == 3
    assert next(row for row in rows if row["contribution_id"] == saved["contribution_id"]) == saved


def test_postgres_usage_attributes_monitored_systems(database):
    repository = SeatBillingRepository(database)
    repository.record_monitor_contribution(interval())
    repository.record_monitor_contribution(interval(system_id="30004759", system_name="S-KSWL"))
    evidence = interval()
    evidence.pop("system_id")
    result = repository.record_client_usage(evidence)
    assert {row["system_id"] for row in result["usages"]} == {"30000142", "30004759"}
    assert sum(row["duration_seconds"] for row in result["usages"]) == 20
    assert repository.record_client_usage(evidence)["created"] is False


def test_unexpected_index_aborts_migration_without_changing_data(database):
    repository = SeatBillingRepository(database)
    saved = repository.record_client_usage(interval())["usage"]
    with database() as connection:
        connection.execute("DROP INDEX IF EXISTS idx_seat_client_usage_identity")
        connection.execute("CREATE INDEX idx_seat_client_usage_identity ON seat_client_usage(account_id)")
        connection.execute("ALTER TABLE seat_client_usage ADD CONSTRAINT legacy_identity UNIQUE (client_id, started_at, ended_at)")
        before = connection.execute("SELECT indexrelid FROM pg_index WHERE indrelid='seat_client_usage'::regclass ORDER BY indexrelid").fetchall()
    with pytest.raises(RuntimeError, match="Unexpected billing index"):
        with database() as connection:
            migrate_seat_billing_schema(connection)
    with database() as connection:
        after = connection.execute("SELECT indexrelid FROM pg_index WHERE indrelid='seat_client_usage'::regclass ORDER BY indexrelid").fetchall()
    assert after == before
    assert repository.list_client_usage()["usage"] == [saved]


def test_duplicate_evidence_aborts_upgrade_without_deleting_rows(database):
    repository = SeatBillingRepository(database)
    repository.record_client_usage(interval())
    with database() as connection:
        constraints = connection.execute("SELECT conname FROM pg_constraint WHERE conrelid='seat_client_usage'::regclass AND contype='u'").fetchall()
        for row in constraints:
            connection.execute('ALTER TABLE seat_client_usage DROP CONSTRAINT "' + row["conname"] + '"')
        connection.execute("DROP INDEX IF EXISTS idx_seat_client_usage_identity")
        connection.execute("""
            INSERT INTO seat_client_usage
            SELECT 'duplicate-id', account_id, key_id, client_id, system_id,
                   started_at, ended_at, duration_seconds, created_at
            FROM seat_client_usage
        """)
    before = repository.list_client_usage()["usage"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        with database() as connection:
            migrate_seat_billing_schema(connection)
    assert repository.list_client_usage()["usage"] == before
