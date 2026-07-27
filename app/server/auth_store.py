"""SQL persistence for server users, sessions, API keys, and EVE identities."""

from __future__ import annotations

import json
from typing import Any, Callable


def migrate_auth_schema(connection: Any) -> None:
    """Create authentication tables using SQLite/PostgreSQL compatible SQL."""
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
    )
    for statement in statements:
        connection.execute(statement)


class AuthRepository:
    """Database operations shared by SQLite and PostgreSQL deployments."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def count_users(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM auth_users").fetchone()
        return int(row["count"] if row is not None else 0)

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
        """Return member accounts explicitly associated with an EVE character."""
        return self._all(
            """
            SELECT DISTINCT users.*
            FROM auth_users AS users
            WHERE users.role = 'member'
              AND (
                EXISTS (
                    SELECT 1 FROM auth_character_whitelist AS whitelist
                    WHERE whitelist.user_id = users.user_id
                      AND whitelist.character_id = ?
                )
                OR EXISTS (
                    SELECT 1 FROM auth_verified_characters AS verified
                    WHERE verified.user_id = users.user_id
                      AND verified.character_id = ?
                )
              )
            ORDER BY users.username_key ASC
            """,
            (int(character_id), int(character_id)),
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM auth_users ORDER BY username_key ASC",
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

    def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM auth_api_keys
            WHERE user_id = ? ORDER BY created_at DESC
            """,
            (user_id,),
        )

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
