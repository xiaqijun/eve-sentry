import sys
import threading
import types
from types import SimpleNamespace

import pytest

from app.server.intel_store import IntelReport
from app.server.postgres_store import (
    POSTGRES_POOL_MAX_SIZE,
    POSTGRES_POOL_MIN_SIZE,
    POSTGRES_POOL_TIMEOUT_SECONDS,
    PostgreSQLIntelStore,
    _PostgresConnection,
    _create_connection_pool,
    _redact_dsn,
)


def test_postgres_store_requires_dsn():
    with pytest.raises(ValueError, match="postgres dsn is required"):
        PostgreSQLIntelStore("")


def test_postgres_dsn_redaction_hides_credentials():
    redacted = _redact_dsn(
        "postgresql://eve_sentry:super-secret@db.internal:5432/eve_sentry"
    )

    assert redacted == "postgresql://***@db.internal:5432/eve_sentry"
    assert "super-secret" not in redacted


def test_postgres_pool_uses_bounded_reusable_connections(monkeypatch):
    captured = {}
    dict_row = object()

    class FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    psycopg_module = types.ModuleType("psycopg")
    rows_module = types.ModuleType("psycopg.rows")
    rows_module.dict_row = dict_row
    pool_module = types.ModuleType("psycopg_pool")
    pool_module.ConnectionPool = FakePool
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_module)

    pool = _create_connection_pool("postgresql://example/eve_sentry")

    assert isinstance(pool, FakePool)
    assert captured == {
        "conninfo": "postgresql://example/eve_sentry",
        "min_size": POSTGRES_POOL_MIN_SIZE,
        "max_size": POSTGRES_POOL_MAX_SIZE,
        "timeout": POSTGRES_POOL_TIMEOUT_SECONDS,
        "kwargs": {"row_factory": dict_row},
        "open": True,
    }


def test_postgres_connection_returns_connection_to_pool_context():
    calls = []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((query, params))
            return "result"

    class FakePoolContext:
        def __enter__(self):
            calls.append("acquire")
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            calls.append("release")
            return False

    connection = _PostgresConnection(FakePoolContext())

    with connection as active:
        assert active.execute("SELECT ?", (1,)) == "result"

    assert calls == ["acquire", ("SELECT %s", (1,)), "release"]
    with pytest.raises(RuntimeError, match="not active"):
        connection.execute("SELECT 1")


def test_postgres_connection_converts_bulk_delete_placeholders():
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def executemany(self, query, params):
            calls.append((query, params))

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    connection = _PostgresConnection(FakePoolContext())
    params = [("old-1",), ("old-2",)]

    with connection as active:
        active.executemany(
            "DELETE FROM intel_reports WHERE report_id = ?",
            params,
        )

    assert calls == [
        ("DELETE FROM intel_reports WHERE report_id = %s", params)
    ]


def test_postgres_report_page_query_uses_converted_keyset_placeholders():
    calls = []

    class EmptyResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((query, params))
            return EmptyResult()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    rows = store._read_report_page_rows(
        anchor=("seen", "received", "report-id"),
        limit=25,
        source="manual",
        system="tama",
    )

    assert rows == []
    query, params = calls[0]
    assert "?" not in query
    assert query.count("%s") == 9
    assert params == (
        "manual",
        "tama",
        "seen",
        "seen",
        "received",
        "seen",
        "received",
        "report-id",
        25,
    )


def test_postgres_startup_hot_set_is_bounded_and_keeps_active_references():
    calls = []

    def report(report_id, timestamp):
        return IntelReport(
            report_id=report_id,
            system="Tama",
            names=[report_id],
            source="manual",
            seen_at=timestamp,
            received_at=timestamp,
        )

    hot_reports = [
        report("hot-new", "2026-07-30T12:02:00+00:00"),
        report("hot-old", "2026-07-30T12:01:00+00:00"),
    ]
    active_report = report("active-old", "2026-01-01T00:00:00+00:00")

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class FakeConnection:
        def execute(self, query, params):
            normalized = " ".join(query.split())
            calls.append((normalized, params))
            if "WHERE report_id IN" in normalized:
                return Result([active_report])
            return Result(hot_reports)

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._hot_report_limit = 2
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._report_from_row = lambda row: row
    store._ensure_system = lambda _system: None

    loaded = store._read_hot_reports({"active-old"})

    assert [item.report_id for item in loaded] == [
        "active-old",
        "hot-old",
        "hot-new",
    ]
    history_query, history_params = calls[0]
    assert "ORDER BY seen_at DESC, received_at DESC, report_id DESC" in history_query
    assert "LIMIT %s" in history_query
    assert history_params == (2,)
    assert "WHERE report_id IN (%s)" in calls[1][0]
    assert calls[1][1] == ("active-old",)


def test_postgres_startup_reads_only_active_intel_rows():
    calls = []
    item = SimpleNamespace(active_id="active-1", system_name="Tama")

    class Result:
        def fetchall(self):
            return [item]

    class FakeConnection:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._active_item_from_row = lambda row: row
    store._ensure_system = lambda _system: None

    loaded = store._read_active_intel()

    assert loaded == {"active-1": item}
    assert "WHERE active = 1" in calls[0][0]


def test_postgres_retention_deletes_in_database_without_loading_history():
    calls = []

    class Result:
        def fetchall(self):
            return [{"report_id": "old-report"}]

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._active_intel = {}
    store._reports = []
    store._alert_cache = {}
    store._lock = threading.RLock()

    removed = store.prune_reports_older_than(
        30,
        now="2026-07-30T00:00:00+00:00",
    )

    assert removed == 1
    assert calls[0][0].startswith("DELETE FROM intel_reports")
    assert "RETURNING report_id" in calls[0][0]
    assert calls[0][1] == ("2026-06-30T00:00:00+00:00",)
