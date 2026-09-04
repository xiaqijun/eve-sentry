"""Durable, hot, and asynchronously refreshed ESI identifier cache."""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


CacheKey = tuple[str, str]
Loader = Callable[[list[str]], Any]
Splitter = Callable[[Any], dict[str, Any]]


@dataclass(slots=True)
class CacheRecord:
    endpoint: str
    entity_key: str
    payload: Any
    fetched_at: float
    expires_at: float
    stale_until: float
    failure_count: int = 0
    next_retry_at: float | None = None
    last_error: str | None = None

    def is_fresh(self, now: float) -> bool:
        return self.expires_at > now

    def is_stale(self, now: float) -> bool:
        return self.expires_at <= now < self.stale_until

    def to_json(self) -> str:
        return json.dumps(
            {
                "endpoint": self.endpoint,
                "entity_key": self.entity_key,
                "payload": self.payload,
                "fetched_at": self.fetched_at,
                "expires_at": self.expires_at,
                "stale_until": self.stale_until,
                "failure_count": self.failure_count,
                "next_retry_at": self.next_retry_at,
                "last_error": self.last_error,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "CacheRecord":
        value = json.loads(raw)
        return cls(
            endpoint=str(value["endpoint"]),
            entity_key=str(value["entity_key"]),
            payload=value.get("payload"),
            fetched_at=float(value["fetched_at"]),
            expires_at=float(value["expires_at"]),
            stale_until=float(value["stale_until"]),
            failure_count=int(value.get("failure_count", 0)),
            next_retry_at=(None if value.get("next_retry_at") is None else float(value["next_retry_at"])),
            last_error=(None if value.get("last_error") is None else str(value["last_error"])),
        )


class DurableStore(Protocol):
    def get_many(self, keys: Sequence[CacheKey]) -> dict[CacheKey, CacheRecord]: ...

    def put_many(self, records: Iterable[CacheRecord]) -> None: ...

    def mark_failure(self, keys: Sequence[CacheKey], error: str, next_retry_at: float) -> None: ...


class HotStore(DurableStore, Protocol):
    def acquire_lock(self, key: CacheKey, ttl_seconds: int) -> str | None: ...

    def release_lock(self, key: CacheKey, token: str) -> None: ...


class MemoryStore:
    """Small deterministic store used for tests and local development."""

    def __init__(self) -> None:
        self._items: dict[CacheKey, CacheRecord] = {}
        self._locks: dict[CacheKey, tuple[str, float]] = {}
        self._lock = threading.RLock()

    def get_many(self, keys: Sequence[CacheKey]) -> dict[CacheKey, CacheRecord]:
        with self._lock:
            return {key: self._items[key] for key in keys if key in self._items}

    def put_many(self, records: Iterable[CacheRecord]) -> None:
        with self._lock:
            for record in records:
                self._items[(record.endpoint, record.entity_key)] = record

    def mark_failure(self, keys: Sequence[CacheKey], error: str, next_retry_at: float) -> None:
        with self._lock:
            for key in keys:
                record = self._items.get(key)
                if record is None:
                    continue
                record.failure_count += 1
                record.last_error = error[:500]
                record.next_retry_at = next_retry_at

    def acquire_lock(self, key: CacheKey, ttl_seconds: int) -> str | None:
        now = time.monotonic()
        with self._lock:
            current = self._locks.get(key)
            if current and current[1] > now:
                return None
            token = secrets.token_urlsafe(18)
            self._locks[key] = (token, now + ttl_seconds)
            return token

    def release_lock(self, key: CacheKey, token: str) -> None:
        with self._lock:
            current = self._locks.get(key)
            if current and current[0] == token:
                self._locks.pop(key, None)


class PostgresStore:
    """PostgreSQL-backed long-term store using a bounded psycopg pool."""

    def __init__(self, dsn: str, *, max_connections: int = 4, table: str = "esi_id_cache") -> None:
        try:
            from psycopg_pool import ConnectionPool
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("PostgreSQL caching requires the 'storage' extra") from exc
        if not table.replace("_", "").isalnum():
            raise ValueError("Invalid PostgreSQL cache table name")
        self.table = table
        self._jsonb = Jsonb
        self._pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=max(1, int(max_connections)), open=True)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    endpoint TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    fetched_at TIMESTAMPTZ NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    stale_until TIMESTAMPTZ NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TIMESTAMPTZ,
                    last_error TEXT,
                    PRIMARY KEY (endpoint, entity_key)
                )
                """
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.table}_refresh ON {self.table} (next_retry_at, expires_at)"
            )

    @staticmethod
    def _dt(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    @staticmethod
    def _timestamp(value: datetime) -> float:
        return value.timestamp()

    def get_many(self, keys: Sequence[CacheKey]) -> dict[CacheKey, CacheRecord]:
        if not keys:
            return {}
        placeholders = ", ".join(["(%s, %s)"] * len(keys))
        values = [part for key in keys for part in key]
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT endpoint, entity_key, payload, fetched_at, expires_at, stale_until, failure_count, next_retry_at, last_error FROM {self.table} WHERE (endpoint, entity_key) IN ({placeholders})",
                values,
            ).fetchall()
        return {
            (row[0], row[1]): CacheRecord(
                endpoint=row[0],
                entity_key=row[1],
                payload=row[2],
                fetched_at=self._timestamp(row[3]),
                expires_at=self._timestamp(row[4]),
                stale_until=self._timestamp(row[5]),
                failure_count=row[6],
                next_retry_at=(None if row[7] is None else self._timestamp(row[7])),
                last_error=row[8],
            )
            for row in rows
        }

    def put_many(self, records: Iterable[CacheRecord]) -> None:
        records = list(records)
        if not records:
            return
        with self._pool.connection() as connection:
            connection.executemany(
                f"""
                INSERT INTO {self.table} (endpoint, entity_key, payload, fetched_at, expires_at, stale_until, failure_count, next_retry_at, last_error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (endpoint, entity_key) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    fetched_at = EXCLUDED.fetched_at,
                    expires_at = EXCLUDED.expires_at,
                    stale_until = EXCLUDED.stale_until,
                    failure_count = EXCLUDED.failure_count,
                    next_retry_at = EXCLUDED.next_retry_at,
                    last_error = EXCLUDED.last_error
                """,
                [
                    (
                        record.endpoint,
                        record.entity_key,
                        self._jsonb(record.payload),
                        self._dt(record.fetched_at),
                        self._dt(record.expires_at),
                        self._dt(record.stale_until),
                        record.failure_count,
                        None if record.next_retry_at is None else self._dt(record.next_retry_at),
                        record.last_error,
                    )
                    for record in records
                ],
            )

    def mark_failure(self, keys: Sequence[CacheKey], error: str, next_retry_at: float) -> None:
        if not keys:
            return
        placeholders = ", ".join(["(%s, %s)"] * len(keys))
        values = [part for key in keys for part in key]
        with self._pool.connection() as connection:
            connection.execute(
                f"UPDATE {self.table} SET failure_count = failure_count + 1, last_error = %s, next_retry_at = %s WHERE (endpoint, entity_key) IN ({placeholders})",
                [error[:500], self._dt(next_retry_at), *values],
            )

    def close(self) -> None:
        self._pool.close()


class RedisHotStore:
    """Redis-backed hot store and distributed per-key refresh lock."""

    def __init__(self, url: str, *, prefix: str = "eve-sentry:esi", socket_timeout: float = 2.0) -> None:
        try:
            from redis import Redis
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("Redis caching requires the 'storage' extra") from exc
        self.prefix = prefix.rstrip(":")
        self._redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            max_connections=16,
            health_check_interval=30,
        )
        self._unlock_script = self._redis.register_script(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        )

    def _key(self, key: CacheKey) -> str:
        return f"{self.prefix}:{key[0]}:{key[1]}"

    def _lock_key(self, key: CacheKey) -> str:
        return f"{self._key(key)}:refresh-lock"

    def get_many(self, keys: Sequence[CacheKey]) -> dict[CacheKey, CacheRecord]:
        if not keys:
            return {}
        pipe = self._redis.pipeline()
        for key in keys:
            pipe.get(self._key(key))
        values = pipe.execute()
        result: dict[CacheKey, CacheRecord] = {}
        for key, value in zip(keys, values, strict=True):
            if value is None:
                continue
            try:
                result[key] = CacheRecord.from_json(value)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                self._redis.delete(self._key(key))
        return result

    def put_many(self, records: Iterable[CacheRecord]) -> None:
        records = list(records)
        if not records:
            return
        now = time.time()
        pipe = self._redis.pipeline()
        for record in records:
            ttl = max(1, int(record.stale_until - now))
            pipe.set(self._key((record.endpoint, record.entity_key)), record.to_json(), ex=ttl)
        pipe.execute()

    def mark_failure(self, keys: Sequence[CacheKey], error: str, next_retry_at: float) -> None:
        records = self.get_many(keys)
        for record in records.values():
            record.failure_count += 1
            record.last_error = error[:500]
            record.next_retry_at = next_retry_at
        self.put_many(records.values())

    def acquire_lock(self, key: CacheKey, ttl_seconds: int) -> str | None:
        token = secrets.token_urlsafe(18)
        if self._redis.set(self._lock_key(key), token, nx=True, ex=max(1, ttl_seconds)):
            return token
        return None

    def release_lock(self, key: CacheKey, token: str) -> None:
        self._unlock_script(keys=[self._lock_key(key)], args=[token])


@dataclass(slots=True)
class _RefreshTask:
    endpoint: str
    entity_key: str
    loader: Loader
    splitter: Splitter
    attempts: int = 0
    next_due: float = 0.0


class IdCacheCoordinator:
    """Read-through two-tier cache with bounded background refresh."""

    def __init__(
        self,
        durable: DurableStore,
        hot: HotStore | None = None,
        *,
        ttl_seconds: float = 86400.0,
        ttl_by_endpoint: Mapping[str, float] | None = None,
        stale_grace_seconds: float = 300.0,
        refresh_interval_seconds: float = 5.0,
        refresh_batch_size: int = 1000,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
        metrics: Any | None = None,
        refresh_gate: Callable[[], None] | None = None,
    ) -> None:
        self.durable = durable
        self.hot = hot
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.ttl_by_endpoint = {
            str(endpoint): max(1.0, float(ttl))
            for endpoint, ttl in (ttl_by_endpoint or {}).items()
        }
        self.stale_grace_seconds = max(0.0, float(stale_grace_seconds))
        self.refresh_interval_seconds = min(10.0, max(5.0, float(refresh_interval_seconds)))
        self.refresh_batch_size = min(1000, max(1, int(refresh_batch_size)))
        self.retry_base_seconds = max(0.1, float(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, float(retry_max_seconds))
        self.metrics = metrics
        self.refresh_gate = refresh_gate
        self._pending: dict[CacheKey, _RefreshTask] = {}
        self._pending_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._refresh_loop, name="esi-cache-refresh", daemon=True)
        self._thread.start()

    @staticmethod
    def key(endpoint: str, entity_key: str | int) -> CacheKey:
        return str(endpoint), str(entity_key)

    def _ttl_for(self, endpoint: str) -> float:
        return self.ttl_by_endpoint.get(endpoint, self.ttl_seconds)

    def fetch_single(
        self,
        endpoint: str,
        entity_key: str | int,
        loader: Callable[[], Any],
    ) -> tuple[Any, str]:
        def batch_loader(_keys: list[str]) -> Any:
            return {str(entity_key): loader()}

        def splitter(payload: Any) -> dict[str, Any]:
            return dict(payload) if isinstance(payload, dict) and str(entity_key) in payload else {str(entity_key): payload}

        values, statuses = self.fetch_batch(endpoint, [str(entity_key)], batch_loader, splitter)
        return values.get(str(entity_key)), statuses.get(str(entity_key), "miss")

    def fetch_batch(
        self,
        endpoint: str,
        entity_keys: Sequence[str | int],
        loader: Loader,
        splitter: Splitter,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        keys = [str(value) for value in entity_keys]
        unique = list(dict.fromkeys(keys))
        cache_keys = [self.key(endpoint, value) for value in unique]
        now = time.time()
        records: dict[CacheKey, CacheRecord] = {}
        statuses: dict[str, str] = {}

        if self.hot is not None:
            try:
                records.update(self.hot.get_many(cache_keys))
                self._metric("hot_hits", sum(1 for record in records.values() if record.is_fresh(now)))
            except Exception:
                self._metric("redis_errors")
        missing: list[str] = []
        stale: dict[str, CacheRecord] = {}
        for value, cache_key in zip(unique, cache_keys, strict=True):
            record = records.get(cache_key)
            if record is not None and record.is_fresh(now):
                statuses[value] = "hit"
                continue
            if record is not None and record.is_stale(now):
                stale[value] = record
            missing.append(value)

        durable_missing_keys = [
            self.key(endpoint, value)
            for value in missing
            if self.key(endpoint, value) not in records or not records[self.key(endpoint, value)].is_fresh(now)
        ]
        if durable_missing_keys:
            try:
                durable_records = self.durable.get_many(durable_missing_keys)
                records.update(durable_records)
                self._metric("durable_hits", sum(1 for record in durable_records.values() if record.is_fresh(now)))
                if self.hot is not None and durable_records:
                    try:
                        self.hot.put_many(durable_records.values())
                    except Exception:
                        self._metric("redis_errors")
            except Exception:
                self._metric("postgres_errors")

        to_load: list[str] = []
        for value in missing:
            record = records.get(self.key(endpoint, value))
            if record is not None and record.is_fresh(now):
                statuses[value] = "hit"
                continue
            if record is not None and record.is_stale(now):
                stale[value] = record
                self._enqueue(endpoint, value, loader, splitter)
                self._metric("stale_hits")
                statuses[value] = "stale"
            else:
                to_load.append(value)

        loaded: dict[str, Any] = {}
        if to_load:
            for value in to_load:
                statuses[value] = "miss"
            try:
                if self.refresh_gate is not None:
                    self.refresh_gate()
                started = time.monotonic()
                loaded = splitter(loader(to_load))
                self._record_upstream(endpoint, time.monotonic() - started)
                fetched_at = time.time()
                new_records = [
                    CacheRecord(
                        endpoint=endpoint,
                        entity_key=value,
                        payload=loaded[value],
                        fetched_at=fetched_at,
                        expires_at=fetched_at + self._ttl_for(endpoint),
                        stale_until=fetched_at + self._ttl_for(endpoint) + self.stale_grace_seconds,
                    )
                    for value in to_load
                    if value in loaded
                ]
                if new_records:
                    try:
                        self.durable.put_many(new_records)
                    except Exception:
                        self._metric("postgres_errors")
                    if self.hot is not None:
                        try:
                            self.hot.put_many(new_records)
                        except Exception:
                            self._metric("redis_errors")
                    for record in new_records:
                        records[self.key(endpoint, record.entity_key)] = record
                        statuses[record.entity_key] = "miss"
            except Exception as exc:
                self._mark_refresh_failure([self.key(endpoint, value) for value in to_load], str(exc), attempts=1)
                if any(value not in stale for value in to_load):
                    raise

        values: dict[str, Any] = {}
        for value in unique:
            record = records.get(self.key(endpoint, value))
            if record is not None and (record.is_fresh(now) or record.is_stale(now)):
                values[value] = record.payload
            elif value in loaded:
                values[value] = loaded[value]
        return values, statuses

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.refresh_interval_seconds + 1.0))
        close = getattr(self.durable, "close", None)
        if close is not None:
            close()

    def health(self) -> dict[str, Any]:
        with self._pending_lock:
            pending = len(self._pending)
        return {
            "enabled": True,
            "durable": type(self.durable).__name__,
            "hot": type(self.hot).__name__ if self.hot is not None else None,
            "pending_refresh": pending,
            "refresh_interval_seconds": self.refresh_interval_seconds,
            "refresh_batch_size": self.refresh_batch_size,
            "ttl_seconds": self.ttl_seconds,
            "ttl_by_endpoint": dict(self.ttl_by_endpoint),
            "stale_grace_seconds": self.stale_grace_seconds,
        }

    def _metric(self, name: str, amount: int = 1) -> None:
        if self.metrics is not None:
            method = getattr(self.metrics, f"record_{name}", None)
            if method is not None:
                method(amount)

    def _enqueue(self, endpoint: str, entity_key: str, loader: Loader, splitter: Splitter) -> None:
        with self._pending_lock:
            key = self.key(endpoint, entity_key)
            if key in self._pending:
                return
            self._pending[key] = _RefreshTask(endpoint, entity_key, loader, splitter)
        self._metric("refresh_queued")

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.refresh_interval_seconds):
            try:
                self._run_refresh_batch()
            except Exception:
                self._metric("refresh_failures")

    def _run_refresh_batch(self) -> None:
        now = time.time()
        with self._pending_lock:
            due = [task for task in self._pending.values() if task.next_due <= now][: self.refresh_batch_size]
        if not due:
            return
        groups: dict[tuple[str, int, int], list[_RefreshTask]] = defaultdict(list)
        for task in due:
            groups[(task.endpoint, id(task.loader), id(task.splitter))].append(task)
        self._metric("refresh_batches")
        for tasks in groups.values():
            self._refresh_group(tasks)

    def _refresh_group(self, tasks: list[_RefreshTask]) -> None:
        acquired: list[tuple[_RefreshTask, str]] = []
        for task in tasks:
            key = self.key(task.endpoint, task.entity_key)
            token = None
            try:
                if self.hot is not None:
                    token = self.hot.acquire_lock(key, int(self.refresh_interval_seconds * 3))
                else:
                    token = "local"
            except Exception:
                self._metric("redis_errors")
            if token is not None:
                acquired.append((task, token))
        if not acquired:
            return
        keys = [task.entity_key for task, _token in acquired]
        task = acquired[0][0]
        try:
            if self.refresh_gate is not None:
                self.refresh_gate()
            started = time.monotonic()
            payloads = task.splitter(task.loader(keys))
            self._record_upstream(task.endpoint, time.monotonic() - started)
            fetched_at = time.time()
            records = [
                CacheRecord(
                    endpoint=task.endpoint,
                    entity_key=key,
                    payload=payloads[key],
                    fetched_at=fetched_at,
                    expires_at=fetched_at + self._ttl_for(task.endpoint),
                    stale_until=fetched_at + self._ttl_for(task.endpoint) + self.stale_grace_seconds,
                )
                for key in keys
                if key in payloads
            ]
            self.durable.put_many(records)
            if self.hot is not None:
                self.hot.put_many(records)
            with self._pending_lock:
                for key in payloads:
                    self._pending.pop(self.key(task.endpoint, key), None)
                for key in set(keys) - set(payloads):
                    pending = self._pending.get(self.key(task.endpoint, key))
                    if pending is not None:
                        pending.attempts += 1
                        pending.next_due = time.time() + self.retry_max_seconds
            self._metric("refresh_success", len(records))
        except Exception as exc:
            for item, _token in acquired:
                item.attempts += 1
                delay = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** (item.attempts - 1)))
                item.next_due = time.time() + delay
                self._mark_refresh_failure([self.key(item.endpoint, item.entity_key)], str(exc), item.attempts)
            self._metric("refresh_retries", len(acquired))
        finally:
            if self.hot is not None:
                for item, token in acquired:
                    try:
                        self.hot.release_lock(self.key(item.endpoint, item.entity_key), token)
                    except Exception:
                        self._metric("redis_errors")

    def _mark_refresh_failure(self, keys: Sequence[CacheKey], error: str, attempts: int) -> None:
        delay = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** max(0, attempts - 1)))
        retry_at = time.time() + delay
        try:
            self.durable.mark_failure(keys, error, retry_at)
        except Exception:
            self._metric("postgres_errors")
        if self.hot is not None:
            try:
                self.hot.mark_failure(keys, error, retry_at)
            except Exception:
                self._metric("redis_errors")
        self._metric("refresh_failures")

    def _record_upstream(self, endpoint: str, duration: float) -> None:
        if self.metrics is not None:
            record = getattr(self.metrics, "record_upstream", None)
            if record is not None:
                record(endpoint, duration)
