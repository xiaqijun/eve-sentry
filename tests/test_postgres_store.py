import sys
import types

import pytest

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
