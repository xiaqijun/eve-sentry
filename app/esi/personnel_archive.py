"""Long-lived personnel repository and leased jobs; not yet wired to runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.esi.personnel_policy import name_key, positive_id, retry_delay, timestamp


MAX_BATCH = 1000
JOB_KINDS = {"resolve", "identity", "affiliation", "corporation", "alliance"}


class NameConflict(ValueError):
    """An already confirmed name is mapped to a different character."""


@dataclass(frozen=True)
class IdentityUpdate:
    character_id: int
    name: str
    fetched_at: float
    seen_at: float


@dataclass(frozen=True)
class AffiliationUpdate:
    character_id: int
    corporation_id: int
    fetched_at: float
    alliance_id: int | None = None
    faction_id: int | None = None


@dataclass(frozen=True)
class OrganizationUpdate:
    kind: str
    entity_id: int
    name: str
    fetched_at: float


@dataclass(frozen=True)
class RefreshLease:
    kind: str
    entity_key: str
    context_key: str
    token: str
    revision: int
    failures: int


def _bounded(values: list[Any]) -> list[Any]:
    if len(values) > MAX_BATCH:
        raise ValueError("batch must contain at most 1000 entries")
    return list(dict.fromkeys(values))


def _job_key(kind: str, entity_key: str | int, context_key: str) -> tuple[str, str, str]:
    if kind not in JOB_KINDS or not isinstance(context_key, str):
        raise ValueError("invalid refresh job key")
    if kind == "resolve":
        key = name_key(entity_key)
    else:
        key = str(positive_id(entity_key))
    return kind, key, context_key


class PersonnelArchive:
    """Use a context-managed DB connection, mapping rows, and %s placeholders.

    PostgreSQL callers should pass pool.connection with dict_row configured.
    sqlite dialect exists only for deterministic repository tests, not production.
    Constructor has no I/O; migrate() is explicit. No method performs ESI requests.
    """

    def __init__(self, connection_factory: Callable[[], Any], *, dialect: str = "postgres") -> None:
        if dialect not in {"postgres", "sqlite"}:
            raise ValueError("unsupported personnel archive dialect")
        self._connection = connection_factory
        self._row_lock = " FOR UPDATE" if dialect == "postgres" else ""
        self._claim_lock = " FOR UPDATE SKIP LOCKED" if dialect == "postgres" else ""

    def migrate(self) -> None:
        schema = Path(__file__).with_name("personnel_schema.sql").read_text(encoding="utf-8")
        with self._connection() as connection:
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)

    def find_names(self, names: list[str]) -> dict[str, dict[str, Any]]:
        keys = _bounded([name_key(name) for name in names])
        if not keys:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT n.name_key, p.* FROM personnel_names n JOIN personnel_profiles p "
                "ON p.character_id = n.character_id WHERE n.active = 1 AND n.name_key IN ("
                + ",".join(["%s"] * len(keys)) + ")", tuple(keys),
            ).fetchall()
        return {row["name_key"]: self._profile(row) for row in rows}

    def get_profiles(self, character_ids: list[int]) -> dict[int, dict[str, Any]]:
        ids = _bounded([positive_id(value) for value in character_ids])
        if not ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM personnel_profiles WHERE character_id IN ("
                + ",".join(["%s"] * len(ids)) + ")", tuple(ids),
            ).fetchall()
        return {row["character_id"]: self._profile(row) for row in rows}

    @staticmethod
    def _profile(row: Any) -> dict[str, Any]:
        result = dict(row)
        payload = result.pop("affiliation_json", None)
        if payload is not None:
            result.update(json.loads(payload))
        return result

    def save_identity(self, update: IdentityUpdate) -> bool:
        """Persist confirmed identity, never raw/fuzzy OCR guesses."""
        with self._connection() as connection:
            return self._identity(connection, update)

    def _identity(self, connection: Any, update: IdentityUpdate) -> bool:
        character_id = positive_id(update.character_id)
        key = name_key(update.name)
        fetched_at, seen_at = timestamp(update.fetched_at), timestamp(update.seen_at)
        connection.execute(
            "INSERT INTO personnel_profiles (character_id,name,name_checked_at,first_seen_at,last_seen_at) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (character_id) DO NOTHING",
            (character_id, update.name.strip(), fetched_at, seen_at, seen_at),
        )
        current = connection.execute(
            "SELECT * FROM personnel_profiles WHERE character_id = %s" + self._row_lock,
            (character_id,),
        ).fetchone()
        # Monotonic observation times, independent of identity refresh freshness.
        connection.execute(
            "UPDATE personnel_profiles SET first_seen_at = CASE WHEN first_seen_at > %s THEN %s ELSE first_seen_at END, "
            "last_seen_at = CASE WHEN last_seen_at < %s THEN %s ELSE last_seen_at END WHERE character_id = %s",
            (seen_at, seen_at, seen_at, seen_at, character_id),
        )
        if current["name_checked_at"] > fetched_at or (
            current["name_checked_at"] == fetched_at and current["name"] != update.name.strip()
        ):
            return False
        connection.execute(
            "INSERT INTO personnel_names (name_key,character_id,verified_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (name_key) DO NOTHING", (key, character_id, fetched_at),
        )
        owner = connection.execute(
            "SELECT character_id FROM personnel_names WHERE name_key = %s" + self._row_lock, (key,),
        ).fetchone()
        if owner["character_id"] != character_id:
            raise NameConflict("confirmed name belongs to another character")
        connection.execute(
            "UPDATE personnel_names SET active = 0 WHERE character_id = %s AND name_key <> %s",
            (character_id, key),
        )
        connection.execute(
            "UPDATE personnel_names SET active = 1, verified_at = %s WHERE name_key = %s",
            (fetched_at, key),
        )
        changed = current["name"] != update.name.strip()
        connection.execute(
            "UPDATE personnel_profiles SET name = %s, name_checked_at = %s, revision = revision + %s "
            "WHERE character_id = %s", (update.name.strip(), fetched_at, int(changed), character_id),
        )
        return True

    def save_affiliation(self, update: AffiliationUpdate) -> bool:
        with self._connection() as connection:
            return self._affiliation(connection, update)

    def _affiliation(self, connection: Any, update: AffiliationUpdate) -> bool:
        character_id = positive_id(update.character_id)
        payload = {
            "corporation_id": positive_id(update.corporation_id),
            "alliance_id": positive_id(update.alliance_id) if update.alliance_id is not None else None,
            "faction_id": positive_id(update.faction_id) if update.faction_id is not None else None,
        }
        fetched_at = timestamp(update.fetched_at)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if connection.execute(
            "SELECT character_id FROM personnel_profiles WHERE character_id = %s" + self._row_lock,
            (character_id,),
        ).fetchone() is None:
            raise ValueError("affiliation requires a confirmed identity")
        # A strictly newer field timestamp prevents late workers undoing a change.
        result = connection.execute(
            "UPDATE personnel_profiles SET affiliation_json = %s, affiliation_fetched_at = %s, "
            "revision = revision + CASE WHEN affiliation_json = %s THEN 0 ELSE 1 END "
            "WHERE character_id = %s AND (affiliation_fetched_at IS NULL OR affiliation_fetched_at < %s)",
            (serialized, fetched_at, serialized, character_id, fetched_at),
        )
        return result.rowcount == 1

    def save_organization(self, update: OrganizationUpdate) -> bool:
        with self._connection() as connection:
            return self._organization(connection, update)

    def _organization(self, connection: Any, update: OrganizationUpdate) -> bool:
        if update.kind not in {"corporation", "alliance"}:
            raise ValueError("invalid organization kind")
        positive_id(update.entity_id)
        name_key(update.name)
        fetched_at = timestamp(update.fetched_at)
        result = connection.execute(
            "INSERT INTO personnel_organizations (kind,entity_id,name,fetched_at) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (kind,entity_id) DO UPDATE SET name = excluded.name, fetched_at = excluded.fetched_at "
            "WHERE personnel_organizations.fetched_at < excluded.fetched_at",
            (update.kind, update.entity_id, update.name.strip(), fetched_at),
        )
        return result.rowcount == 1

    def get_organizations(self, kind: str, entity_ids: list[int]) -> dict[int, dict[str, Any]]:
        if kind not in {"corporation", "alliance"}:
            raise ValueError("invalid organization kind")
        ids = _bounded([positive_id(value) for value in entity_ids])
        if not ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM personnel_organizations WHERE kind = %s AND entity_id IN ("
                + ",".join(["%s"] * len(ids)) + ")", (kind, *ids),
            ).fetchall()
        return {row["entity_id"]: dict(row) for row in rows}

    def request_refresh(
        self, kind: str, entity_key: str | int, *, priority: int, due_at: float,
        context_key: str = "",
        promote_only: bool = False,
    ) -> None:
        key = _job_key(kind, entity_key, context_key)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 5:
            raise ValueError("priority must be 0-5")
        timestamp(due_at)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO personnel_refresh_jobs (kind,entity_key,context_key,priority,next_due_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (kind,entity_key,context_key) DO UPDATE SET "
                "priority = CASE WHEN excluded.priority < personnel_refresh_jobs.priority "
                "THEN excluded.priority ELSE personnel_refresh_jobs.priority END, "
                "next_due_at = CASE WHEN personnel_refresh_jobs.failures > 0 THEN personnel_refresh_jobs.next_due_at "
                "WHEN excluded.next_due_at < personnel_refresh_jobs.next_due_at "
                "THEN excluded.next_due_at ELSE personnel_refresh_jobs.next_due_at END, "
                "revision = personnel_refresh_jobs.revision + 1 "
                "WHERE excluded.priority < personnel_refresh_jobs.priority "
                "OR (NOT %s AND personnel_refresh_jobs.failures = 0 "
                "AND excluded.next_due_at < personnel_refresh_jobs.next_due_at)",
                (*key, priority, due_at, promote_only),
            )

    def claim(
        self, *, now: float, limit: int = 100, lease_seconds: float = 30,
        minimum_priority: int = 0, maximum_priority: int = 5, oldest_first: bool = False,
        kind: str | None = None,
    ) -> list[RefreshLease]:
        timestamp(now)
        timestamp(lease_seconds)
        if isinstance(limit, bool) or not isinstance(limit, int) or lease_seconds <= 0 or not 1 <= limit <= MAX_BATCH:
            raise ValueError("invalid lease or claim size")
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (minimum_priority, maximum_priority)) or not 0 <= minimum_priority <= maximum_priority <= 5:
            raise ValueError("invalid priority range")
        order = "next_due_at, priority" if oldest_first else "priority, next_due_at"
        if kind is not None and kind not in JOB_KINDS:
            raise ValueError("invalid job kind")
        leases = []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM personnel_refresh_jobs WHERE next_due_at <= %s AND lease_until <= %s "
                "AND priority BETWEEN %s AND %s " + ("AND kind = %s " if kind else "")
                + "ORDER BY " + order + ", kind, entity_key, context_key LIMIT %s"
                + self._claim_lock, (now, now, minimum_priority, maximum_priority, *((kind,) if kind else ()), limit),
            ).fetchall()
            for row in rows:
                token = uuid4().hex
                key = (row["kind"], row["entity_key"], row["context_key"])
                connection.execute(
                    "UPDATE personnel_refresh_jobs SET lease_token = %s, lease_until = %s, leased_revision = revision "
                    "WHERE kind = %s AND entity_key = %s AND context_key = %s", (token, now + lease_seconds, *key),
                )
                leases.append(RefreshLease(*key, token, row["revision"], row["failures"]))
        return leases

    def finish(
        self, lease: RefreshLease, *, now: float, next_due_at: float, next_priority: int,
        update: IdentityUpdate | AffiliationUpdate | OrganizationUpdate | None = None,
    ) -> bool:
        """Fence result writes and acknowledge a task in the same transaction."""
        timestamp(now)
        if (isinstance(next_priority, bool) or not isinstance(next_priority, int)
                or timestamp(next_due_at) < now or not 0 <= next_priority <= 5):
            raise ValueError("invalid next refresh schedule")
        with self._connection() as connection:
            current = self._leased(connection, lease, now)
            if current is None:
                return False
            if update is not None:
                self._apply_leased_update(connection, lease, update)
            unchanged = current["revision"] == lease.revision
            connection.execute(
                "UPDATE personnel_refresh_jobs SET lease_token = '', lease_until = 0, failures = 0, last_error = '', "
                "next_due_at = %s, priority = %s WHERE kind = %s AND entity_key = %s AND context_key = %s",
                (next_due_at if unchanged else current["next_due_at"],
                 next_priority if unchanged else current["priority"], lease.kind, lease.entity_key, lease.context_key),
            )
        return True

    def _apply_leased_update(self, connection: Any, lease: RefreshLease, update: Any) -> None:
        if isinstance(update, IdentityUpdate):
            matches = (lease.kind == "identity" and lease.entity_key == str(update.character_id)) or (
                lease.kind == "resolve" and lease.entity_key == name_key(update.name)
            )
            write = self._identity
        elif isinstance(update, AffiliationUpdate):
            matches = lease.kind == "affiliation" and lease.entity_key == str(update.character_id)
            write = self._affiliation
        elif isinstance(update, OrganizationUpdate):
            matches = lease.kind == update.kind and lease.entity_key == str(update.entity_id)
            write = self._organization
        else:
            raise ValueError("unsupported refresh result")
        if not matches:
            raise ValueError("result does not match leased entity")
        write(connection, update)

    def fail(
        self, lease: RefreshLease, *, now: float, error_code: str, jitter: float = 0.5,
        retry_after: float = 0.0,
    ) -> bool:
        """Persist only bounded error categories, never upstream response bodies."""
        timestamp(now)
        if error_code not in {"timeout", "throttled", "upstream", "not_found", "conflict", "storage"}:
            raise ValueError("use a safe error category")
        with self._connection() as connection:
            current = self._leased(connection, lease, now)
            if current is None:
                return False
            failures = current["failures"] + 1
            due = now + retry_delay(failures, jitter=jitter, retry_after=retry_after)
            connection.execute(
                "UPDATE personnel_refresh_jobs SET failures = %s, last_error = %s, next_due_at = %s, "
                "lease_token = '', lease_until = 0 WHERE kind = %s AND entity_key = %s AND context_key = %s",
                (failures, error_code, due, lease.kind, lease.entity_key, lease.context_key),
            )
        return True

    def _leased(self, connection: Any, lease: RefreshLease, now: float) -> Any:
        return connection.execute(
            "SELECT * FROM personnel_refresh_jobs WHERE kind = %s AND entity_key = %s AND context_key = %s "
            "AND lease_token = %s AND lease_until > %s AND leased_revision = %s" + self._row_lock,
            (lease.kind, lease.entity_key, lease.context_key, lease.token, now, lease.revision),
        ).fetchone()

    def page(self, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Keyset pagination for resumable maintenance, never load the full archive."""
        if not 1 <= limit <= MAX_BATCH or after_id < 0:
            raise ValueError("invalid page")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM personnel_profiles WHERE character_id > %s ORDER BY character_id LIMIT %s",
                (after_id, limit),
            ).fetchall()
        return [self._profile(row) for row in rows]

    def observe(self, character_ids: list[int], seen_at: float) -> None:
        ids = _bounded([positive_id(value) for value in character_ids])
        timestamp(seen_at)
        if not ids:
            return
        with self._connection() as connection:
            connection.execute(
                "UPDATE personnel_profiles SET last_seen_at = %s WHERE last_seen_at < %s AND character_id IN ("
                + ",".join(["%s"] * len(ids)) + ")", (seen_at, seen_at, *ids),
            )

    def statistics(self, now: float) -> dict[str, Any]:
        with self._connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM personnel_profiles").fetchone()["count"]
            jobs = connection.execute(
                "SELECT priority, kind, COUNT(*) AS count, MIN(next_due_at) AS oldest_due_at "
                "FROM personnel_refresh_jobs WHERE next_due_at <= %s AND lease_until <= %s GROUP BY priority, kind",
                (timestamp(now), timestamp(now)),
            ).fetchall()
        return {"profiles": count, "due_by_priority": [dict(row) for row in jobs]}
