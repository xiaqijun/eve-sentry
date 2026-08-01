import sys
import threading
import types
from types import SimpleNamespace

import pytest

from app.core.active_intel import ActiveIntelItem
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


@pytest.mark.parametrize("method_name", ["list_reports", "list_observations"])
def test_postgres_limited_history_uses_keyset_query(method_name):
    report = IntelReport(
        report_id="report-1",
        system="Tama",
        names=["Pilot"],
        source="manual",
    )
    page_calls = []
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._report_page_items = lambda **kwargs: (
        page_calls.append(kwargs) or ([report], "next")
    )
    store._read_reports = lambda **_kwargs: pytest.fail(
        "limited history must not read the full report table"
    )

    result = getattr(store, method_name)(limit=1)

    assert len(result) == 1
    assert page_calls[0]["limit"] == 1


@pytest.mark.parametrize(
    ("kwargs", "expected_clause", "expected_params"),
    [
        (
            {"character_id": 9001},
            "character_ids_json::jsonb @> %s::jsonb",
            ('[9001]',),
        ),
        ({"system_id": 30000142}, "system_id = %s", (30000142,)),
    ],
)
def test_postgres_entity_report_queries_filter_in_database(
    kwargs,
    expected_clause,
    expected_params,
):
    calls = []

    class EmptyResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return EmptyResult()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    assert store._read_reports(**kwargs) == []
    assert expected_clause in calls[0][0]
    assert calls[0][1] == expected_params


@pytest.mark.parametrize(
    ("returned_row", "deleted"),
    [(None, False), ({"report_id": "report-1"}, True)],
)
def test_postgres_delete_report_respects_active_database_references(
    returned_row,
    deleted,
):
    calls = []

    class Result:
        def fetchone(self):
            return returned_row

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

    assert store._delete_report("report-1") is deleted
    query, params = calls[0]
    assert "NOT EXISTS" in query
    assert "jsonb_exists" in query
    assert "active = 1" in query
    assert "RETURNING report.report_id" in query
    assert params == ("report-1",)


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


def test_postgres_ocr_snapshot_persists_old_system_as_inactive():
    persisted_rows = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    old_item = ActiveIntelItem(
        active_id="old-active",
        source="eve-sentry-detector",
        source_instance="EVE - Pilot A",
        system_name="S-KSWL",
        name="Alice",
        metadata={"client_id": "detector-client:test:pilot-a"},
        first_seen_at="2026-07-03T10:00:00+00:00",
        last_seen_at="2026-07-03T10:00:00+00:00",
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._active_intel = {old_item.active_id: old_item}
    store._reports = []
    store._resolver = None
    store._enricher = None
    store._ocr_missing_counts = {}
    store._alert_cache = {}
    store._lock = threading.RLock()
    store._connect = FakeConnection
    store._active_row = lambda item: item.to_dict()
    store._upsert_active_intel_rows = (
        lambda _connection, rows: persisted_rows.extend(rows)
    )
    store._reset_ocr_alert_cooldown = lambda _item: None

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "names": [],
            "seen_at": "2026-07-03T10:00:10+00:00",
        }
    )

    assert result["expired"] == 1
    assert old_item.active is False
    assert old_item.left_at == "2026-07-03T10:00:10+00:00"
    assert old_item.metadata["left_reason"] == "system_changed"
    assert persisted_rows == [old_item.to_dict()]


def test_postgres_retention_deletes_in_database_without_loading_history():
    calls = []

    class Result:
        rowcount = 1

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
    assert calls[0][0].startswith("WITH active_report_refs AS")
    assert "NOT EXISTS" in calls[0][0]
    assert "jsonb_array_elements_text" in calls[0][0]
    assert "active = 1" in calls[0][0]
    assert calls[0][1] == ("2026-06-30T00:00:00+00:00",)


def test_postgres_prunes_only_inactive_intel_older_than_cutoff():
    calls = []

    class Result:
        rowcount = 1

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
    store._lock = threading.RLock()

    removed = store.prune_inactive_active_intel_older_than(
        30,
        now="2026-07-30T00:00:00+00:00",
    )

    assert removed == 1
    query, params = calls[0]
    assert query.startswith("DELETE FROM active_intel")
    assert "active = 0" in query
    assert "NULLIF(cleared_at, '')" in query
    assert "NULLIF(left_at, '')" in query
    assert "::timestamptz < %s::timestamptz" in query
    assert "RETURNING active_id" not in query
    assert params == ("2026-06-30T00:00:00+00:00",)


@pytest.mark.parametrize("retention_days", [-1, True, 1.5])
def test_postgres_inactive_intel_retention_rejects_invalid_days(retention_days):
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    with pytest.raises(ValueError):
        store.prune_inactive_active_intel_older_than(retention_days)
