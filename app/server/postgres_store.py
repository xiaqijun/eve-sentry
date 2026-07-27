"""PostgreSQL-backed hostile intel store."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.server.auth_store import migrate_auth_schema
from app.server.intel_store import IntelStore, StarSystem
from app.server.sqlite_store import SQLiteIntelStore


POSTGRES_POOL_MIN_SIZE = 2
POSTGRES_POOL_MAX_SIZE = 8
POSTGRES_POOL_TIMEOUT_SECONDS = 5.0


class PostgreSQLIntelStore(SQLiteIntelStore):
    """Persist intel reports in PostgreSQL while keeping the IntelStore API."""

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
    ) -> None:
        self._postgres_dsn = str(dsn or "").strip()
        if not self._postgres_dsn:
            raise ValueError("postgres dsn is required")
        self._postgres_safe_dsn = _redact_dsn(self._postgres_dsn)
        self._postgres_pool = _create_connection_pool(self._postgres_dsn)
        try:
            super().__init__(
                db_path="postgresql",
                import_json_path=import_json_path,
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

    def close(self, *, wait: bool = True) -> None:
        """Stop background work and close reusable PostgreSQL connections."""
        try:
            super().close(wait=wait)
        finally:
            self._postgres_pool.close()

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
                    confidence DOUBLE PRECISION,
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
                    confidence DOUBLE PRECISION,
                    source_observation_ids_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            for column, definition in (
                ("active_id", "TEXT NOT NULL DEFAULT ''"),
                ("source", "TEXT NOT NULL DEFAULT ''"),
                ("source_instance", "TEXT NOT NULL DEFAULT ''"),
                ("system", "TEXT NOT NULL DEFAULT 'Unknown'"),
                ("system_id", "BIGINT"),
                ("target_type", "TEXT NOT NULL DEFAULT 'character'"),
                ("name", "TEXT NOT NULL DEFAULT ''"),
                ("character_id", "BIGINT"),
                ("raw_text", "TEXT NOT NULL DEFAULT ''"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("first_seen_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_seen_at", "TEXT NOT NULL DEFAULT ''"),
                ("expires_at", "TEXT NOT NULL DEFAULT ''"),
                ("left_at", "TEXT NOT NULL DEFAULT ''"),
                ("cleared_at", "TEXT NOT NULL DEFAULT ''"),
                ("active", "INTEGER NOT NULL DEFAULT 1"),
                ("seen_count", "INTEGER NOT NULL DEFAULT 1"),
                ("confidence", "DOUBLE PRECISION"),
                ("source_observation_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                self._ensure_column(connection, "active_intel", column, definition)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS client_heartbeats (
                    client_id TEXT PRIMARY KEY,
                    client_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    heartbeat_interval_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._ensure_column(
                connection,
                "client_heartbeats",
                "heartbeat_interval_seconds",
                "DOUBLE PRECISION NOT NULL DEFAULT 0",
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

    def _connect(self) -> "_PostgresConnection":
        return _PostgresConnection(self._postgres_pool.connection())

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


class _PostgresConnection:
    """Small compatibility wrapper for SQLite-style store methods."""

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
