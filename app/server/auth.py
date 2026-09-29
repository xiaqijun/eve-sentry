"""Authentication and EVE character authorization services."""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.server.auth_store import AuthRepository


SESSION_COOKIE_NAME = "eve_sentry_session"
SESSION_HOURS = 12
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_IP_FAILURE_LIMIT = 25
logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Base error carrying an HTTP-compatible status and stable code."""

    def __init__(self, message: str, status: int = 401, code: str = "unauthorized"):
        super().__init__(message)
        self.status = int(status)
        self.code = code


@dataclass(frozen=True)
class AuthPrincipal:
    """Authenticated browser session or API-key identity."""

    user_id: str
    username: str
    display_name: str
    role: str
    auth_type: str
    api_key_id: str = ""
    api_key_type: str = ""
    session_hash: str = ""
    csrf_token: str = ""
    integration: str = ""
    account_id: str = ""
    permissions: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_read_only(self) -> bool:
        return self.api_key_type == "service_readonly"

    @property
    def is_seat(self) -> bool:
        return self.integration == "seat"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "auth_type": self.auth_type,
            "api_key_id": self.api_key_id,
            "api_key_type": self.api_key_type,
        }
        if self.integration:
            payload.update(
                {
                    "integration": self.integration,
                    "account_id": self.account_id,
                    "permissions": list(self.permissions),
                }
            )
        return payload


class AuthService:
    """Manage users, sessions, and API keys.

    API keys are independent credentials.  The former EVE identity/risk
    control flow has been retired; account access is governed by the user and
    key status only.
    """

    def __init__(
        self,
        repository: AuthRepository,
        resolver: Any,
        enforce_requests: bool = True,
        seat_auth_mode: str = "off",
    ) -> None:
        self.repository = repository
        self.resolver = resolver
        self.enforce_requests = bool(enforce_requests)
        normalized_seat_mode = str(seat_auth_mode or "off").strip().casefold()
        if normalized_seat_mode not in {"off", "enforce"}:
            raise ValueError("seat_auth_mode must be off or enforce")
        self.seat_auth_mode = normalized_seat_mode
        self._login_failures: dict[str, list[float]] = {}
        self._login_lock = threading.Lock()
        self._authorization_generation = 0
        self._authorization_change_lock = threading.Lock()
        self._authorization_change_listeners: set[Callable[[], None]] = set()

    @property
    def authorization_generation(self) -> int:
        """Return the current in-process authorization revision."""
        with self._authorization_change_lock:
            return self._authorization_generation

    def add_authorization_change_listener(self, listener: Callable[[], None]) -> None:
        """Register an idempotent callback for principal-invalidating changes."""
        with self._authorization_change_lock:
            self._authorization_change_listeners.add(listener)

    def _notify_authorization_changed(self) -> None:
        with self._authorization_change_lock:
            self._authorization_generation += 1
            listeners = tuple(self._authorization_change_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("Authorization change listener failed")

    def notify_external_authorization_changed(self) -> None:
        """Invalidate active streams after an external credential change."""
        self._notify_authorization_changed()

    def ensure_bootstrap_admin(self, username: str, password: str) -> dict[str, Any]:
        """Create the first administrator only while the user table is empty."""
        if self.repository.count_users() > 0:
            user = self.repository.user_by_username(_username_key(username))
            return _public_user(user) if user else {}
        return self.create_user(
            username=username,
            password=password,
            display_name=username,
            role="admin",
            must_change_password=True,
            actor_user_id="bootstrap",
        )

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str = "",
        role: str = "member",
        must_change_password: bool = True,
        actor_user_id: str = "",
    ) -> dict[str, Any]:
        username = str(username or "").strip()
        if len(username) < 3 or len(username) > 64:
            raise AuthError("username must contain 3 to 64 characters", 400, "invalid_username")
        role = str(role or "member").strip().casefold()
        if role not in {"admin", "member"}:
            raise AuthError("role must be admin or member", 400, "invalid_role")
        password_hash = _hash_password(
            password if role == "admin" or password else secrets.token_urlsafe(48)
        )
        if role == "member":
            must_change_password = False
        now = _now_iso()
        record = {
            "user_id": uuid.uuid4().hex,
            "username": username,
            "username_key": _username_key(username),
            "display_name": str(display_name or username).strip() or username,
            "role": role,
            "status": "active",
            "password_hash": password_hash,
            "must_change_password": bool(must_change_password),
            "disabled_reason": "",
            "created_at": now,
            "updated_at": now,
        }
        try:
            user = self.repository.create_user(record)
        except Exception as exc:
            if "unique" in str(exc).casefold() or "duplicate" in str(exc).casefold():
                raise AuthError("username already exists", 409, "username_exists") from exc
            raise
        self._audit(actor_user_id, user["user_id"], "user.created", {"role": role})
        return _public_user(user)

    def login(self, username: str, password: str, remote_key: str = "") -> dict[str, Any]:
        username_key = _username_key(username)
        remote_key = str(remote_key or "").strip()
        ip_throttle_key = f"ip:{remote_key}" if remote_key else ""
        pair_throttle_key = f"pair:{remote_key}:{username_key}"
        if ip_throttle_key:
            self._check_login_rate(ip_throttle_key, LOGIN_IP_FAILURE_LIMIT)
        self._check_login_rate(pair_throttle_key, LOGIN_FAILURE_LIMIT)
        user = self.repository.user_by_username(username_key)
        if user is None or not _verify_password(password, str(user["password_hash"])):
            if ip_throttle_key:
                self._record_login_failure(ip_throttle_key)
            self._record_login_failure(pair_throttle_key)
            raise AuthError("invalid username or password", 401, "invalid_credentials")
        if str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        self._clear_login_failures(pair_throttle_key)
        return self._create_browser_session(user, "password")

    def _create_browser_session(
        self,
        user: dict[str, Any],
        method: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        record = {
            "token_hash": _secret_hash(token),
            "user_id": str(user["user_id"]),
            "csrf_token": csrf_token,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=SESSION_HOURS)).isoformat(),
            "last_seen_at": now.isoformat(),
        }
        self.repository.create_session(record)
        self._audit(
            str(user["user_id"]),
            str(user["user_id"]),
            "session.login",
            {"method": method, **(details or {})},
        )
        return {
            "session_token": token,
            "csrf_token": csrf_token,
            "expires_at": record["expires_at"],
            "user": _public_user(user),
        }

    def logout(self, principal: AuthPrincipal) -> None:
        if principal.session_hash:
            self.repository.delete_session(principal.session_hash)
            self._audit(principal.user_id, principal.user_id, "session.logout", {})
            self._notify_authorization_changed()

    def authenticate_session(self, token: str) -> AuthPrincipal:
        token_hash = _secret_hash(token)
        session = self.repository.session_by_hash(token_hash)
        if session is None:
            raise AuthError("session is invalid or expired", 401, "invalid_session")
        if str(session["expires_at"]) <= _now_iso():
            self.repository.delete_session(token_hash)
            raise AuthError("session is invalid or expired", 401, "invalid_session")
        user = self.repository.user_by_id(str(session["user_id"]))
        if user is None or str(user.get("status")) != "active":
            self.repository.delete_session(token_hash)
            raise AuthError("user is disabled", 403, "user_disabled")
        self.repository.touch_session(token_hash, _now_iso())
        return AuthPrincipal(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role=str(user["role"]),
            auth_type="session",
            session_hash=token_hash,
            csrf_token=str(session["csrf_token"]),
        )

    def authenticate_api_key(
        self,
        secret: str,
    ) -> AuthPrincipal:
        secret_hash = _secret_hash(secret)
        key = self.repository.api_key_by_hash(secret_hash)
        if key is None:
            if self.seat_auth_mode != "off":
                return self._authenticate_seat_api_key(secret_hash)
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        if str(key.get("status")) != "active":
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        user = self.repository.user_by_id(str(key["user_id"]))
        if user is None or str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        key_type = str(key.get("key_type") or "desktop")
        self.repository.mark_api_key_used(str(key["key_id"]), _now_iso())
        return AuthPrincipal(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role=str(user["role"]),
            auth_type="api_key",
            api_key_id=str(key["key_id"]),
            api_key_type=key_type,
        )

    def _authenticate_seat_api_key(self, secret_hash: str) -> AuthPrincipal:
        key = self.repository.seat_integration_key_by_hash(secret_hash)
        if key is None:
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        key_id = str(key.get("key_id") or "")
        account_id = str(key.get("account_id") or "")
        binding = self.repository.external_account_by_id("seat", account_id)
        error: AuthError | None = None
        if str(key.get("status") or "") != "active":
            error = AuthError("Seat API key is revoked", 401, "invalid_api_key")
        elif binding is None:
            error = AuthError("Seat account is not bound", 403, "seat_account_unmapped")
        elif str(binding.get("status") or "") != "active":
            error = AuthError("Seat account binding is disabled", 403, "seat_account_disabled")
        else:
            user = self.repository.user_by_id(str(binding.get("user_id") or ""))
            if user is None or str(user.get("status") or "") != "active":
                error = AuthError("user is disabled", 403, "user_disabled")
        if error is not None:
            raise error
        user = self.repository.user_by_id(str(binding["user_id"]))
        assert user is not None
        principal = AuthPrincipal(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role=str(user["role"]),
            auth_type="api_key",
            api_key_id=key_id,
            api_key_type="seat",
            integration="seat",
            account_id=account_id,
            permissions=tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in (key.get("permissions") or [])
                        if str(value).strip() in {"monitor", "alert"}
                    }
                )
            ),
        )
        return principal

    def is_principal_active(self, principal: AuthPrincipal) -> bool:
        """Return whether an already-authenticated SSE principal remains valid."""
        user = self.repository.user_by_id(principal.user_id)
        if user is None or str(user.get("status")) != "active":
            return False
        if principal.is_seat:
            key = self.repository.seat_integration_key_by_id(principal.api_key_id)
            if not key or str(key.get("status") or "") != "active":
                return False
            binding = self.repository.external_account_by_id("seat", principal.account_id)
            return bool(
                binding
                and str(binding.get("status") or "") == "active"
                and str(binding.get("user_id") or "") == principal.user_id
            )
        if principal.auth_type == "api_key":
            key = self.repository.api_key_by_id(principal.api_key_id)
            return bool(key and str(key.get("status")) == "active")
        if principal.auth_type == "session":
            session = self.repository.session_by_hash(principal.session_hash)
            return bool(session and str(session.get("expires_at")) > _now_iso())
        return False

    def bind_seat_account(
        self,
        account_id: str,
        user_id: str,
        actor_user_id: str,
        revision: int = 1,
    ) -> dict[str, Any]:
        """Bind one Seat account to one local user, without implicit merging."""
        account_id = str(account_id or "").strip()
        user_id = str(user_id or "").strip()
        if not account_id:
            raise AuthError("Seat account ID is required", 400, "invalid_seat_account")
        user = self.repository.user_by_id(user_id)
        if user is None:
            raise AuthError("user not found", 404, "user_not_found")
        if str(user.get("status") or "") != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        now = _now_iso()
        result = self.repository.bind_external_account(
            {
                "provider": "seat",
                "account_id": account_id,
                "user_id": user_id,
                "status": "active",
                "revision": max(1, int(revision)),
                "created_at": now,
                "updated_at": now,
            },
            self._audit_record(
                actor_user_id,
                user_id,
                "seat_account.bound",
                {"provider": "seat", "account_id": account_id},
                now=now,
            ),
        )
        conflict = str(result.get("conflict") or "")
        if conflict and conflict != "already_bound":
            raise AuthError("Seat account binding conflicts with an existing binding", 409, "seat_account_conflict")
        if result.get("bound"):
            self._notify_authorization_changed()
        return result

    def set_seat_account_status(
        self,
        account_id: str,
        active: bool,
        actor_user_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """Disable or enable a Seat binding and invalidate active streams."""
        binding = self.repository.external_account_by_id("seat", str(account_id))
        if binding is None:
            raise AuthError("Seat account binding not found", 404, "seat_account_not_found")
        now = _now_iso()
        status = "active" if active else "disabled"
        result = self.repository.set_external_account_status(
            "seat",
            str(account_id),
            status,
            now,
            self._audit_record(
                actor_user_id,
                str(binding.get("user_id") or ""),
                "seat_account.enabled" if active else "seat_account.disabled",
                {"provider": "seat", "account_id": str(account_id), "reason": str(reason or "")},
                now=now,
            ),
        )
        if result and result.get("changed"):
            self._notify_authorization_changed()
        return result or binding

    def change_password(
        self,
        principal: AuthPrincipal,
        current_password: str,
        new_password: str,
    ) -> dict[str, Any]:
        user = self.repository.user_by_id(principal.user_id)
        if user is None or not _verify_password(current_password, str(user["password_hash"])):
            raise AuthError("current password is incorrect", 400, "invalid_password")
        updated = self.repository.update_user(
            principal.user_id,
            {
                "password_hash": _hash_password(new_password),
                "must_change_password": 0,
                "updated_at": _now_iso(),
            },
        )
        self._audit(principal.user_id, principal.user_id, "password.changed", {})
        return _public_user(updated)

    def create_api_key(
        self,
        user_id: str,
        name: str,
        actor_user_id: str,
        key_type: str = "desktop",
    ) -> dict[str, Any]:
        user = self.repository.user_by_id(user_id)
        if user is None:
            raise AuthError("user not found", 404, "user_not_found")
        if str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        if key_type not in {"desktop", "service_readonly"}:
            raise AuthError("invalid API key type", 400, "invalid_key_type")
        secret = f"eve_{secrets.token_urlsafe(36)}"
        now = _now_iso()
        record = {
            "key_id": uuid.uuid4().hex,
            "user_id": user_id,
            "name": str(name or "Device").strip()[:80] or "Device",
            "key_prefix": secret[:12],
            "key_hash": _secret_hash(secret),
            "key_type": key_type,
            "status": "active",
            "identity_verified": True,
            "created_at": now,
            "last_used_at": "",
            "revoked_at": "",
            "revoked_reason": "",
        }
        key = self.repository.create_api_key(record)
        self._audit(actor_user_id, user_id, "api_key.created", {
            "key_id": record["key_id"], "key_type": key_type, "name": record["name"],
        })
        return {**self._public_api_key_record(key), "secret": secret}

    def revoke_api_key(self, key_id: str, principal: AuthPrincipal) -> None:
        if self.repository.seat_integration_key_by_id(key_id) is not None:
            raise AuthError(
                "Seat client keys must be revoked by GloryNavy_Seat",
                410,
                "seat_key_management_required",
            )
        key = self.repository.api_key_by_id(key_id)
        if key is None:
            raise AuthError("API key not found", 404, "api_key_not_found")
        if not principal.is_admin and str(key["user_id"]) != principal.user_id:
            raise AuthError("administrator access is required", 403, "forbidden")
        if str(key.get("status")) != "active":
            raise AuthError("API key is already revoked", 409, "api_key_already_revoked")
        reason = (
            "revoked by administrator"
            if principal.is_admin and str(key["user_id"]) != principal.user_id
            else "revoked by user"
        )
        self.repository.revoke_api_key(key_id, _now_iso(), reason)
        self._audit(principal.user_id, str(key["user_id"]), "api_key.revoked", {"key_id": key_id})
        self._notify_authorization_changed()

    def enable_api_key(self, key_id: str, principal: AuthPrincipal) -> None:
        if self.repository.seat_integration_key_by_id(key_id) is not None:
            raise AuthError(
                "Seat client keys are managed by GloryNavy_Seat",
                410,
                "seat_key_management_required",
            )
        key = self.repository.api_key_by_id(key_id)
        if key is None:
            raise AuthError("API key not found", 404, "api_key_not_found")
        if not principal.is_admin and str(key["user_id"]) != principal.user_id:
            raise AuthError("administrator access is required", 403, "forbidden")
        if str(key.get("status")) == "active":
            raise AuthError("API key is already active", 409, "api_key_already_active")
        if str(key.get("revoked_reason")) not in {
            "revoked by user",
            "revoked by administrator",
        }:
            raise AuthError(
                "this API key cannot be restored",
                409,
                "api_key_restore_forbidden",
            )
        user = self.repository.user_by_id(str(key["user_id"]))
        if user is None:
            raise AuthError("user not found", 404, "user_not_found")
        if str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        self.repository.enable_api_key(key_id)
        self._audit(principal.user_id, str(key["user_id"]), "api_key.enabled", {"key_id": key_id})
        self._notify_authorization_changed()

    def delete_api_key(self, key_id: str, principal: AuthPrincipal) -> None:
        if self.repository.seat_integration_key_by_id(key_id) is not None:
            raise AuthError(
                "Seat client keys must be deleted by GloryNavy_Seat",
                410,
                "seat_key_management_required",
            )
        key = self.repository.api_key_by_id(key_id)
        if key is None:
            raise AuthError("API key not found", 404, "api_key_not_found")
        if not principal.is_admin and str(key["user_id"]) != principal.user_id:
            raise AuthError("administrator access is required", 403, "forbidden")
        if str(key.get("status")) != "revoked":
            raise AuthError(
                "active API keys must be revoked before deletion",
                409,
                "api_key_must_be_revoked",
            )
        self.repository.delete_api_key(key_id)
        self._audit(principal.user_id, str(key["user_id"]), "api_key.deleted", {"key_id": key_id})

    def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        legacy = [
            self._public_api_key_record(item)
            for item in self.repository.list_api_keys(user_id)
        ]
        seat_keys = [
            self._public_seat_api_key_record(item)
            for item in self.repository.list_seat_integration_keys(user_id)
        ]
        return sorted(
            [*legacy, *seat_keys],
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )

    def close(self, *, wait: bool = True) -> None:
        """Keep shutdown call sites compatible; no background auth worker remains."""
        return None

    def list_users(self) -> list[dict[str, Any]]:
        users = []
        for item in self.repository.list_users():
            user = _public_user(item)
            user["keys"] = self.list_api_keys(str(item["user_id"]))
            users.append(user)
        return users

    def list_users_with_api_keys(self) -> list[dict[str, Any]]:
        """Return only public user and API-key fields for client management."""
        user_rows, key_rows = self.repository.list_users_and_api_keys()
        keys_by_user: dict[str, list[dict[str, Any]]] = {}
        for item in key_rows:
            user_id = str(item.get("user_id") or "")
            keys_by_user.setdefault(user_id, []).append(
                self._public_api_key_record(item)
            )
        for item in self.repository.list_seat_integration_keys_with_users():
            user_id = str(item.get("user_id") or "")
            if user_id:
                keys_by_user.setdefault(user_id, []).append(
                    self._public_seat_api_key_record(item)
                )
        users = []
        for item in user_rows:
            user = _public_user(item)
            user["keys"] = keys_by_user.get(str(item.get("user_id") or ""), [])
            users.append(user)
        return users

    def _public_api_key_record(self, key: dict[str, Any]) -> dict[str, Any]:
        result = _public_api_key(key)
        return result

    def _public_seat_api_key_record(self, key: dict[str, Any]) -> dict[str, Any]:
        return {
            field: key.get(field)
            for field in (
                "key_id", "user_id", "name", "key_prefix", "key_type", "status",
                "created_at", "last_used_at", "revoked_at", "revoked_reason",
                "account_id", "permissions",
            )
        }

    def set_user_status(
        self,
        user_id: str,
        active: bool,
        actor_user_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        user = self.repository.user_by_id(user_id)
        if user is None:
            raise AuthError("user not found", 404, "user_not_found")
        now = _now_iso()
        if not active:
            self.repository.disable_user_and_keys(
                user_id,
                reason or "disabled by administrator",
                now,
                self._audit_record(actor_user_id, user_id, "user.disabled", {"reason": reason}, now=now),
            )
        else:
            self.repository.update_user(user_id, {
                "status": "active", "disabled_reason": "", "updated_at": now,
            })
            self._audit(actor_user_id, user_id, "user.enabled", {})
        updated = _public_user(self.repository.user_by_id(user_id))
        self._notify_authorization_changed()
        return updated

    def delete_user(self, user_id: str, actor_user_id: str) -> None:
        """Delete a user and all owned authentication records."""
        user = self.repository.user_by_id(user_id)
        if user is None:
            raise AuthError("user not found", 404, "user_not_found")
        if user_id == actor_user_id:
            raise AuthError(
                "the current administrator cannot be deleted",
                409,
                "cannot_delete_self",
            )
        if str(user.get("role")) == "admin":
            admin_count = sum(
                1
                for item in self.repository.list_users()
                if str(item.get("role")) == "admin"
            )
            if admin_count <= 1:
                raise AuthError(
                    "the last administrator cannot be deleted",
                    409,
                    "cannot_delete_last_admin",
                )
        now = _now_iso()
        self.repository.delete_user_and_dependencies(
            user_id,
            self._audit_record(
                actor_user_id,
                user_id,
                "user.deleted",
                {
                    "username": str(user.get("username") or ""),
                    "role": str(user.get("role") or ""),
                },
                now=now,
            ),
        )
        self._notify_authorization_changed()

    def reset_password(
        self,
        user_id: str,
        password: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        if self.repository.user_by_id(user_id) is None:
            raise AuthError("user not found", 404, "user_not_found")
        updated = self.repository.update_user(user_id, {
            "password_hash": _hash_password(password),
            "must_change_password": 1,
            "updated_at": _now_iso(),
        })
        self.repository.delete_user_sessions(user_id)
        self._audit(actor_user_id, user_id, "password.reset", {})
        self._notify_authorization_changed()
        return _public_user(updated)

    def _audit(
        self,
        actor_user_id: str,
        target_user_id: str,
        action: str,
        details: dict[str, Any],
    ) -> None:
        self.repository.add_audit(
            self._audit_record(actor_user_id, target_user_id, action, details)
        )

    def _audit_record(
        self,
        actor_user_id: str,
        target_user_id: str,
        action: str,
        details: dict[str, Any],
        now: str | None = None,
    ) -> dict[str, Any]:
        return {
            "audit_id": uuid.uuid4().hex,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "action": action,
            "details": details,
            "created_at": now or _now_iso(),
        }

    def _check_login_rate(self, key: str, limit: int = LOGIN_FAILURE_LIMIT) -> None:
        cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
        with self._login_lock:
            failures = [item for item in self._login_failures.get(key, []) if item >= cutoff]
            if failures:
                self._login_failures[key] = failures
            else:
                self._login_failures.pop(key, None)
            if len(failures) >= max(1, int(limit)):
                raise AuthError("too many login attempts", 429, "login_rate_limited")

    def _record_login_failure(self, key: str) -> None:
        with self._login_lock:
            self._login_failures.setdefault(key, []).append(time.monotonic())

    def _clear_login_failures(self, key: str) -> None:
        with self._login_lock:
            self._login_failures.pop(key, None)


def _password_hasher():
    try:
        from argon2 import PasswordHasher
    except ImportError as exc:
        raise RuntimeError("server authentication requires argon2-cffi") from exc
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def _hash_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 12:
        raise AuthError("password must contain at least 12 characters", 400, "weak_password")
    return str(_password_hasher().hash(password))


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(_password_hasher().verify(password_hash, str(password or "")))
    except Exception:
        return False


def _public_user(user: dict[str, Any] | None) -> dict[str, Any]:
    if not user:
        return {}
    return {
        key: user.get(key)
        for key in (
            "user_id", "username", "display_name", "role", "status",
            "must_change_password", "disabled_reason", "created_at", "updated_at",
        )
    }


def _public_api_key(key: dict[str, Any]) -> dict[str, Any]:
    return {
        field: key.get(field)
        for field in (
            "key_id", "user_id", "name", "key_prefix", "key_type", "status",
            "created_at", "last_used_at", "revoked_at", "revoked_reason",
        )
    }


def _username_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _secret_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
