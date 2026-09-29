"""SQL persistence for server users, sessions, and API keys.

The legacy EVE identity tables are still created for non-destructive upgrades,
but no authorization path reads or writes them anymore.
"""

from __future__ import annotations

import json
from typing import Any, Callable


def migrate_auth_schema(connection: Any) -> None:
    """Create authentication tables in PostgreSQL."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS auth_users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            username_key TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'member',
            status TEXT NOT NULL DEFAULT 'active',
            password_hash TEXT NOT NULL,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_api_keys (
            key_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            key_type TEXT NOT NULL DEFAULT 'desktop',
            status TEXT NOT NULL DEFAULT 'active',
            identity_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL DEFAULT '',
            revoked_at TEXT NOT NULL DEFAULT '',
            revoked_reason TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_auth_api_keys_user
        ON auth_api_keys(user_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            csrf_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
        ON auth_sessions(user_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_allowed_corporations (
            corporation_id BIGINT PRIMARY KEY,
            corporation_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_character_whitelist (
            user_id TEXT NOT NULL,
            character_id BIGINT NOT NULL,
            character_name TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, character_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_verified_characters (
            user_id TEXT NOT NULL,
            character_id BIGINT NOT NULL,
            character_name TEXT NOT NULL,
            corporation_id BIGINT,
            corporation_name TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (user_id, character_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_audit_log (
            audit_id TEXT PRIMARY KEY,
            actor_user_id TEXT NOT NULL DEFAULT '',
            target_user_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_auth_audit_created
        ON auth_audit_log(created_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_identity_jobs (
            job_id TEXT PRIMARY KEY,
            api_key_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            client_id TEXT NOT NULL DEFAULT '',
            names_hash TEXT NOT NULL,
            names_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'queued',
            result_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL DEFAULT '',
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_until TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE (api_key_id, names_hash)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_auth_identity_jobs_ready
        ON auth_identity_jobs(status, next_attempt_at, lease_until)
        """,
        """
        CREATE TABLE IF NOT EXISTS seat_integration_keys (
            key_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            permissions_json TEXT NOT NULL DEFAULT '[]',
            protocol_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT '',
            revoked_reason TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS seat_integration_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            key_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_external_accounts (
            provider TEXT NOT NULL,
            account_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, account_id),
            UNIQUE (provider, user_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_auth_external_accounts_user
        ON auth_external_accounts(provider, user_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)
    # The Seat projection was introduced after the initial integration table.
    # Add its non-secret ownership/version fields without rewriting existing
    # rows when a deployment is upgraded in place.
    for table, column, definition in (
        ("seat_integration_keys", "account_id", "TEXT NOT NULL DEFAULT ''"),
        ("seat_integration_keys", "protocol_version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if _auth_column_exists(connection, table, column):
            continue
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def _auth_column_exists(connection: Any, table: str, column: str) -> bool:
    """Check an auth table column on both SQLite test stores and PostgreSQL."""
    module_name = str(type(connection).__module__ or "").casefold()
    if "sqlite" in module_name:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(row[1]) == column for row in rows)
    result = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = ?
          AND column_name = ?
        """,
        (table, column),
    )
    fetchall = getattr(result, "fetchall", None)
    if callable(fetchall):
        rows = fetchall()
    else:
        row = result.fetchone()
        rows = [row] if row is not None else []
    return bool(rows)


class AuthRepository:
    """Database operations used by PostgreSQL deployments."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def count_users(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM auth_users").fetchone()
        return int(row["count"] if row is not None else 0)

    def setting(self, key: str) -> str | None:
        row = self._one(
            "SELECT setting_value FROM auth_settings WHERE setting_key = ?",
            (str(key),),
        )
        return str(row["setting_value"]) if row is not None else None

    def set_setting(self, key: str, value: str, updated_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                (str(key), str(value), str(updated_at)),
            )

    def create_user(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_users (
                    user_id, username, username_key, display_name, role, status,
                    password_hash, must_change_password, disabled_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["user_id"], record["username"], record["username_key"],
                    record["display_name"], record["role"], record["status"],
                    record["password_hash"], int(record["must_change_password"]),
                    record["disabled_reason"], record["created_at"], record["updated_at"],
                ),
            )
        return self.user_by_id(record["user_id"]) or {}

    def user_by_username(self, username_key: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM auth_users WHERE username_key = ?",
            (username_key,),
        )

    def user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM auth_users WHERE user_id = ?", (user_id,))

    def users_for_character_id(self, character_id: int) -> list[dict[str, Any]]:
        """Return member accounts explicitly assigned to an EVE character."""
        username_keys = (
            f"eve-{int(character_id)}",
            f"eve-member-{int(character_id)}",
        )
        return self._all(
            """
            SELECT DISTINCT users.*
            FROM auth_users AS users
            WHERE users.role = 'member'
              AND (
                users.username_key IN (?, ?)
                OR
                EXISTS (
                    SELECT 1 FROM auth_character_whitelist AS whitelist
                    WHERE whitelist.user_id = users.user_id
                      AND whitelist.character_id = ?
                )
              )
            ORDER BY users.username_key ASC
            """,
            (*username_keys, int(character_id)),
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM auth_users ORDER BY username_key ASC",
        )

    def list_users_and_api_keys(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return management identity and key rows without EVE relationship data."""
        with self._connect() as connection:
            user_rows = connection.execute(
                "SELECT * FROM auth_users ORDER BY username_key ASC"
            ).fetchall()
            key_rows = connection.execute(
                "SELECT * FROM auth_api_keys ORDER BY created_at DESC"
            ).fetchall()
        return (
            [dict(row) for row in user_rows],
            [dict(row) for row in key_rows],
        )

    def update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "display_name", "role", "status", "password_hash",
            "must_change_password", "disabled_reason", "updated_at",
        }
        fields = [key for key in changes if key in allowed]
        if not fields:
            return self.user_by_id(user_id)
        assignments = ", ".join(f"{field} = ?" for field in fields)
        params = tuple(changes[field] for field in fields) + (user_id,)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE auth_users SET {assignments} WHERE user_id = ?",
                params,
            )
        return self.user_by_id(user_id)

    def delete_user_and_dependencies(
        self,
        user_id: str,
        audit: dict[str, Any],
    ) -> None:
        """Delete one user and owned credentials while preserving the audit trail."""
        with self._connect() as connection:
            for table in (
                "auth_sessions",
                "auth_api_keys",
                "auth_character_whitelist",
                "auth_verified_characters",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE user_id = ?",
                    (user_id,),
                )
            connection.execute("DELETE FROM auth_users WHERE user_id = ?", (user_id,))
            self._insert_audit(connection, audit)

    def create_api_key(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_api_keys (
                    key_id, user_id, name, key_prefix, key_hash, key_type,
                    status, identity_verified, created_at, last_used_at,
                    revoked_at, revoked_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["key_id"], record["user_id"], record["name"],
                    record["key_prefix"], record["key_hash"], record["key_type"],
                    record["status"], int(record["identity_verified"]),
                    record["created_at"], record["last_used_at"],
                    record["revoked_at"], record["revoked_reason"],
                ),
            )
        return self.api_key_by_id(record["key_id"]) or {}

    def api_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM auth_api_keys WHERE key_hash = ?",
            (key_hash,),
        )

    def api_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM auth_api_keys WHERE key_id = ?", (key_id,))

    def seat_integration_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Return the public Seat key projection matched by its secret hash."""
        row = self._one(
            "SELECT * FROM seat_integration_keys WHERE key_hash = ?",
            (str(key_hash),),
        )
        return self._seat_integration_key_from_row(row)

    def external_account_by_id(
        self,
        provider: str,
        account_id: str,
    ) -> dict[str, Any] | None:
        return self._one(
            """
            SELECT * FROM auth_external_accounts
            WHERE provider = ? AND account_id = ?
            """,
            (str(provider), str(account_id)),
        )

    def external_account_by_user(
        self,
        provider: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        return self._one(
            """
            SELECT * FROM auth_external_accounts
            WHERE provider = ? AND user_id = ?
            """,
            (str(provider), str(user_id)),
        )

    def bind_external_account(
        self,
        record: dict[str, Any],
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        """Create an explicit one-to-one external account binding."""
        provider = str(record["provider"])
        account_id = str(record["account_id"])
        user_id = str(record["user_id"])
        with self._connect() as connection:
            existing_account = connection.execute(
                """
                SELECT * FROM auth_external_accounts
                WHERE provider = ? AND account_id = ?
                """,
                (provider, account_id),
            ).fetchone()
            existing_user = connection.execute(
                """
                SELECT * FROM auth_external_accounts
                WHERE provider = ? AND user_id = ?
                """,
                (provider, user_id),
            ).fetchone()
            if existing_account is not None:
                existing = dict(existing_account)
                if str(existing.get("user_id")) != user_id:
                    return {"bound": False, "conflict": "account_already_bound", **existing}
                if existing_user is not None and str(existing_user["account_id"]) != account_id:
                    return {"bound": False, "conflict": "user_already_bound", **dict(existing_user)}
                return {"bound": False, "conflict": "already_bound", **existing}
            if existing_user is not None:
                return {
                    "bound": False,
                    "conflict": "user_already_bound",
                    **dict(existing_user),
                }
            connection.execute(
                """
                INSERT INTO auth_external_accounts (
                    provider, account_id, user_id, status, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    account_id,
                    user_id,
                    str(record.get("status") or "active"),
                    int(record.get("revision") or 1),
                    str(record["created_at"]),
                    str(record["updated_at"]),
                ),
            )
            self._insert_audit(connection, audit)
            created = connection.execute(
                """
                SELECT * FROM auth_external_accounts
                WHERE provider = ? AND account_id = ?
                """,
                (provider, account_id),
            ).fetchone()
        return {"bound": True, **(dict(created) if created is not None else record)}

    def set_external_account_status(
        self,
        provider: str,
        account_id: str,
        status: str,
        updated_at: str,
        audit: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Set binding status and advance its authorization revision."""
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM auth_external_accounts
                WHERE provider = ? AND account_id = ?
                """,
                (str(provider), str(account_id)),
            ).fetchone()
            if current is None:
                return None
            current_dict = dict(current)
            next_status = str(status)
            changed = str(current_dict.get("status") or "") != next_status
            revision = int(current_dict.get("revision") or 1) + (1 if changed else 0)
            if changed:
                connection.execute(
                    """
                    UPDATE auth_external_accounts
                    SET status = ?, revision = ?, updated_at = ?
                    WHERE provider = ? AND account_id = ?
                    """,
                    (
                        next_status,
                        revision,
                        str(updated_at),
                        str(provider),
                        str(account_id),
                    ),
                )
                if audit is not None:
                    self._insert_audit(connection, audit)
            updated = connection.execute(
                """
                SELECT * FROM auth_external_accounts
                WHERE provider = ? AND account_id = ?
                """,
                (str(provider), str(account_id)),
            ).fetchone()
        return {
            **(dict(updated) if updated is not None else current_dict),
            "changed": changed,
        }

    def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM auth_api_keys
            WHERE user_id = ? ORDER BY created_at DESC
            """,
            (user_id,),
        )

    def list_seat_integration_keys(self, user_id: str = "") -> list[dict[str, Any]]:
        """Return Seat-issued key projections joined to explicit bindings."""
        query = """
            SELECT keys.*, accounts.user_id AS bound_user_id
            FROM seat_integration_keys AS keys
            JOIN auth_external_accounts AS accounts
              ON accounts.provider = 'seat'
             AND accounts.account_id = keys.account_id
            WHERE accounts.user_id = ?
            ORDER BY keys.created_at DESC
        """
        rows = self._all(query, (str(user_id),))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._seat_integration_key_from_row(row) or {}
            item["user_id"] = str(row.get("bound_user_id") or "")
            item["key_type"] = "seat"
            item["last_used_at"] = ""
            result.append(item)
        return result

    def list_seat_integration_keys_with_users(self) -> list[dict[str, Any]]:
        """Return all active or historical Seat projections with local owners."""
        rows = self._all(
            """
            SELECT keys.*, accounts.user_id AS bound_user_id
            FROM seat_integration_keys AS keys
            JOIN auth_external_accounts AS accounts
              ON accounts.provider = 'seat'
             AND accounts.account_id = keys.account_id
            ORDER BY keys.created_at DESC
            """,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = self._seat_integration_key_from_row(row) or {}
            item["user_id"] = str(row.get("bound_user_id") or "")
            item["key_type"] = "seat"
            item["last_used_at"] = ""
            result.append(item)
        return result

    def mark_api_key_used(self, key_id: str, used_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auth_api_keys SET last_used_at = ? WHERE key_id = ?",
                (used_at, key_id),
            )

    def mark_api_key_verified(self, key_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auth_api_keys SET identity_verified = 1 WHERE key_id = ?",
                (key_id,),
            )

    def revoke_api_key(self, key_id: str, revoked_at: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auth_api_keys
                SET status = 'revoked', revoked_at = ?, revoked_reason = ?
                WHERE key_id = ?
                """,
                (revoked_at, reason, key_id),
            )

    def revoke_api_key_and_audit(
        self,
        key_id: str,
        revoked_at: str,
        reason: str,
        audit: dict[str, Any],
    ) -> bool:
        """Revoke one API key and persist the identity audit atomically."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_api_keys
                SET status = 'revoked', revoked_at = ?, revoked_reason = ?
                WHERE key_id = ? AND status = 'active'
                """,
                (revoked_at, reason, key_id),
            )
            if int(getattr(cursor, "rowcount", 0)) != 1:
                return False
            self._insert_audit(connection, audit)
        return True

    def revoke_desktop_keys_and_audit(
        self,
        user_id: str,
        revoked_at: str,
        reason: str,
        audit: dict[str, Any],
    ) -> None:
        """Revoke active desktop keys without disabling the owning user."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auth_api_keys
                SET status = 'revoked', revoked_at = ?, revoked_reason = ?
                WHERE user_id = ? AND key_type = 'desktop' AND status = 'active'
                """,
                (revoked_at, reason, user_id),
            )
            self._insert_audit(connection, audit)

    def enable_api_key(self, key_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auth_api_keys
                SET status = 'active', revoked_at = '', revoked_reason = ''
                WHERE key_id = ?
                """,
                (key_id,),
            )

    def delete_api_key(self, key_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM auth_api_keys WHERE key_id = ?", (key_id,))

    def create_seat_integration_key(
        self,
        record: dict[str, Any],
        operation_id: str,
        request_hash: str,
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        """Project one Seat key, or return the operation's idempotent record."""
        normalized_idempotency = str(operation_id or "").strip()
        try:
            with self._connect() as connection:
                if normalized_idempotency:
                    existing = connection.execute(
                        """
                        SELECT idempotency_key, request_hash, key_id
                        FROM seat_integration_idempotency
                        WHERE idempotency_key = ?
                        """,
                        (normalized_idempotency,),
                    ).fetchone()
                    if existing is not None:
                        key = connection.execute(
                            """
                            SELECT * FROM seat_integration_keys WHERE key_id = ?
                            """,
                            (str(existing["key_id"]),),
                        ).fetchone()
                        return {
                            "key": self._seat_integration_key_from_row(
                                dict(key) if key is not None else None
                            ),
                            "created": False,
                            "idempotency_conflict": (
                                str(existing["request_hash"]) != str(request_hash)
                            ),
                        }
                connection.execute(
                    """
                    INSERT INTO seat_integration_keys (
                        key_id, account_id, name, key_prefix, key_hash,
                        permissions_json, protocol_version, status, created_at,
                        revoked_at, revoked_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["key_id"],
                        record["account_id"],
                        record["name"],
                        record["key_prefix"],
                        record["key_hash"],
                        record["permissions_json"],
                        int(record["protocol_version"]),
                        record["status"],
                        record["created_at"],
                        record["revoked_at"],
                        record["revoked_reason"],
                    ),
                )
                if normalized_idempotency:
                    connection.execute(
                        """
                        INSERT INTO seat_integration_idempotency (
                            idempotency_key, request_hash, key_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            normalized_idempotency,
                            str(request_hash),
                            record["key_id"],
                            record["created_at"],
                        ),
                    )
                self._insert_audit(connection, audit)
                key = connection.execute(
                    "SELECT * FROM seat_integration_keys WHERE key_id = ?",
                    (record["key_id"],),
                ).fetchone()
                return {
                    "key": self._seat_integration_key_from_row(
                        dict(key) if key is not None else None
                    ),
                    "created": True,
                    "idempotency_conflict": False,
                }
        except Exception:
            # A concurrent request may have won the idempotency insert. Resolve
            # that race without exposing a database error to the integration.
            if normalized_idempotency:
                existing = self._one(
                    """
                    SELECT idempotency_key, request_hash, key_id
                    FROM seat_integration_idempotency
                    WHERE idempotency_key = ?
                    """,
                    (normalized_idempotency,),
                )
                if existing is not None:
                    key = self.seat_integration_key_by_id(str(existing["key_id"]))
                    return {
                        "key": key,
                        "created": False,
                        "idempotency_conflict": (
                            str(existing["request_hash"]) != str(request_hash)
                        ),
                    }
            raise

    def seat_integration_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM seat_integration_keys WHERE key_id = ?",
            (str(key_id),),
        )
        return self._seat_integration_key_from_row(row)

    def revoke_seat_integration_key(
        self,
        key_id: str,
        revoked_at: str,
        reason: str,
        audit: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Revoke a Seat key idempotently and return its public record."""
        with self._connect() as connection:
            key = connection.execute(
                "SELECT * FROM seat_integration_keys WHERE key_id = ?",
                (str(key_id),),
            ).fetchone()
            if key is None:
                return None
            key_dict = dict(key)
            changed = str(key_dict.get("status") or "") != "revoked"
            if changed:
                connection.execute(
                    """
                    UPDATE seat_integration_keys
                    SET status = 'revoked', revoked_at = ?, revoked_reason = ?
                    WHERE key_id = ?
                    """,
                    (str(revoked_at), str(reason), str(key_id)),
                )
                self._insert_audit(connection, audit)
            updated = connection.execute(
                "SELECT * FROM seat_integration_keys WHERE key_id = ?",
                (str(key_id),),
            ).fetchone()
        return {
            **(self._seat_integration_key_from_row(
                dict(updated) if updated is not None else key_dict
            ) or {}),
            "changed": changed,
        }

    def create_session(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["token_hash"], record["user_id"], record["csrf_token"],
                    record["created_at"], record["expires_at"], record["last_seen_at"],
                ),
            )

    def session_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM auth_sessions WHERE token_hash = ?",
            (token_hash,),
        )

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (token_hash,),
            )

    def delete_user_sessions(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    def touch_session(self, token_hash: str, seen_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (seen_at, token_hash),
            )

    def list_allowed_corporations(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM auth_allowed_corporations ORDER BY corporation_id ASC"
        )

    def upsert_allowed_corporation(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_allowed_corporations (
                    corporation_id, corporation_name, created_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(corporation_id) DO UPDATE SET
                    corporation_name = excluded.corporation_name
                """,
                (record["corporation_id"], record["corporation_name"], record["created_at"]),
            )

    def delete_allowed_corporation(self, corporation_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_allowed_corporations WHERE corporation_id = ?",
                (corporation_id,),
            )

    def allowed_corporation_ids(self) -> set[int]:
        return {
            int(row["corporation_id"])
            for row in self.list_allowed_corporations()
        }

    def list_whitelist(self, user_id: str) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM auth_character_whitelist
            WHERE user_id = ? ORDER BY character_id ASC
            """,
            (user_id,),
        )

    def upsert_whitelist(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_character_whitelist (
                    user_id, character_id, character_name, note, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, character_id) DO UPDATE SET
                    character_name = excluded.character_name,
                    note = excluded.note
                """,
                (
                    record["user_id"], record["character_id"],
                    record["character_name"], record["note"], record["created_at"],
                ),
            )

    def delete_whitelist(self, user_id: str, character_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM auth_character_whitelist
                WHERE user_id = ? AND character_id = ?
                """,
                (user_id, character_id),
            )

    def whitelist_ids(self, user_id: str) -> set[int]:
        return {int(row["character_id"]) for row in self.list_whitelist(user_id)}

    def list_verified_characters(self, user_id: str) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM auth_verified_characters
            WHERE user_id = ? ORDER BY character_name ASC
            """,
            (user_id,),
        )

    def upsert_verified_character(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_verified_characters (
                    user_id, character_id, character_name, corporation_id,
                    corporation_name, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, character_id) DO UPDATE SET
                    character_name = excluded.character_name,
                    corporation_id = excluded.corporation_id,
                    corporation_name = excluded.corporation_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    record["user_id"], record["character_id"], record["character_name"],
                    record.get("corporation_id"), record["corporation_name"],
                    record["first_seen_at"], record["last_seen_at"],
                ),
            )

    def disable_user_and_keys(
        self,
        user_id: str,
        reason: str,
        now: str,
        audit: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auth_users
                SET status = 'disabled', disabled_reason = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (reason, now, user_id),
            )
            connection.execute(
                """
                UPDATE auth_api_keys
                SET status = 'revoked', revoked_at = ?, revoked_reason = ?
                WHERE user_id = ? AND status = 'active'
                """,
                (now, reason, user_id),
            )
            connection.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            self._insert_audit(connection, audit)

    def add_audit(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            self._insert_audit(connection, record)

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._all(
            """
            SELECT * FROM auth_audit_log
            ORDER BY created_at DESC LIMIT ?
            """,
            (max(1, min(1000, int(limit))),),
        )
        for row in rows:
            try:
                row["details"] = json.loads(str(row.pop("details_json", "{}")))
            except json.JSONDecodeError:
                row["details"] = {}
        return rows

    def ensure_identity_job(self, record: dict[str, Any]) -> dict[str, Any]:
        """Create or return one persistent identity job for a key/input set."""
        identity_inputs = (
            record.get("character_ids") or record.get("names") or []
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_identity_jobs (
                    job_id, api_key_id, user_id, client_id, names_hash, names_json,
                    status, result_json, error_code, error_message,
                    attempt_count, next_attempt_at, lease_owner, lease_until,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(api_key_id, names_hash) DO NOTHING
                """,
                (
                    record["job_id"], record["api_key_id"], record["user_id"],
                    record.get("client_id", ""), record["names_hash"],
                    json.dumps(identity_inputs, ensure_ascii=False),
                    record.get("status", "queued"),
                    json.dumps(record.get("result") or {}, ensure_ascii=False),
                    record.get("error_code", ""), record.get("error_message", ""),
                    int(record.get("attempt_count", 0)),
                    record.get("next_attempt_at", ""),
                    record.get("lease_owner", ""), record.get("lease_until", ""),
                    record["created_at"], record["updated_at"],
                    record.get("completed_at", ""),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM auth_identity_jobs
                WHERE api_key_id = ? AND names_hash = ?
                """,
                (str(record["api_key_id"]), str(record["names_hash"])),
            ).fetchone()
        return self._identity_job_from_row(dict(row) if row is not None else None) or {}

    def identity_job_for_hash(
        self,
        api_key_id: str,
        names_hash: str,
    ) -> dict[str, Any] | None:
        row = self._one(
            """
            SELECT * FROM auth_identity_jobs
            WHERE api_key_id = ? AND names_hash = ?
            """,
            (api_key_id, names_hash),
        )
        return self._identity_job_from_row(row)

    def claim_identity_job(
        self,
        now: str,
        lease_owner: str,
        lease_until: str,
    ) -> dict[str, Any] | None:
        """Claim one due or lease-expired identity job with optimistic locking."""
        candidate = self._one(
            """
            SELECT * FROM auth_identity_jobs
            WHERE (
                status IN ('queued', 'retrying') AND next_attempt_at <= ?
            ) OR (
                status = 'processing' AND lease_until <= ?
            )
            ORDER BY next_attempt_at ASC, created_at ASC
            LIMIT 1
            """,
            (now, now),
        )
        if candidate is None:
            return None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_identity_jobs
                SET status = 'processing', lease_owner = ?, lease_until = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE job_id = ? AND status = ? AND updated_at = ?
                """,
                (
                    lease_owner, lease_until, now, candidate["job_id"],
                    candidate["status"], candidate["updated_at"],
                ),
            )
            if int(getattr(cursor, "rowcount", 0)) != 1:
                return None
        claimed = self._one(
            "SELECT * FROM auth_identity_jobs WHERE job_id = ?",
            (candidate["job_id"],),
        )
        return self._identity_job_from_row(claimed)

    def retry_identity_job(
        self,
        job_id: str,
        lease_owner: str,
        next_attempt_at: str,
        error_code: str,
        error_message: str,
        updated_at: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_identity_jobs
                SET status = 'retrying', next_attempt_at = ?,
                    error_code = ?, error_message = ?, lease_owner = '',
                    lease_until = '', updated_at = ?
                WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                """,
                (
                    next_attempt_at, error_code, error_message, updated_at,
                    job_id, lease_owner,
                ),
            )
            return int(getattr(cursor, "rowcount", 0)) == 1

    def complete_identity_job(
        self,
        job_id: str,
        lease_owner: str,
        status: str,
        result: dict[str, Any],
        error_code: str,
        error_message: str,
        completed_at: str,
        audit: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_identity_jobs
                SET status = ?, result_json = ?, error_code = ?,
                    error_message = ?, lease_owner = '', lease_until = '',
                    updated_at = ?, completed_at = ?
                WHERE job_id = ? AND status = 'processing' AND lease_owner = ?
                """,
                (
                    status, json.dumps(result or {}, ensure_ascii=False),
                    error_code, error_message, completed_at, completed_at,
                    job_id, lease_owner,
                ),
            )
            if int(getattr(cursor, "rowcount", 0)) != 1:
                return False
            if audit is not None:
                self._insert_audit(connection, audit)
            return True

    def _identity_job_from_row(
        self,
        row: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            identity_inputs = json.loads(
                str(item.pop("names_json", "[]"))
            )
        except json.JSONDecodeError:
            identity_inputs = []
        if isinstance(identity_inputs, list) and all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in identity_inputs
        ):
            item["character_ids"] = identity_inputs
            item["names"] = []
        else:
            item["character_ids"] = []
            item["names"] = identity_inputs if isinstance(identity_inputs, list) else []
        try:
            item["result"] = json.loads(str(item.pop("result_json", "{}")))
        except json.JSONDecodeError:
            item["result"] = {}
        return item

    def _insert_audit(self, connection: Any, record: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO auth_audit_log (
                audit_id, actor_user_id, target_user_id, action,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record["audit_id"], record.get("actor_user_id", ""),
                record.get("target_user_id", ""), record["action"],
                json.dumps(record.get("details", {}), ensure_ascii=False),
                record["created_at"],
            ),
        )

    def _seat_integration_key_from_row(
        self,
        row: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            permissions = json.loads(str(item.pop("permissions_json", "[]")))
        except json.JSONDecodeError:
            permissions = []
        item["permissions"] = [
            str(value).strip()
            for value in permissions
            if str(value).strip()
        ] if isinstance(permissions, list) else []
        item.pop("key_hash", None)
        item.pop("permissions_json", None)
        return item

    def _one(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def _all(
        self,
        query: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            cursor = (
                connection.execute(query, params)
                if params is not None
                else connection.execute(query)
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]
