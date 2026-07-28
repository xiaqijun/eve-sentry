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
from urllib.parse import parse_qs, urlparse

from app.esi.sso import EsiSsoError
from app.server.auth_store import AuthRepository


SESSION_COOKIE_NAME = "eve_sentry_session"
SESSION_HOURS = 12
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
ESI_LOGIN_STATE_SECONDS = 5 * 60

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Base error carrying an HTTP-compatible status and stable code."""

    def __init__(self, message: str, status: int = 401, code: str = "unauthorized"):
        super().__init__(message)
        self.status = int(status)
        self.code = code


class IdentityUnavailableError(AuthError):
    """Raised when ESI cannot safely decide whether a character is allowed."""

    def __init__(self, message: str):
        super().__init__(message, status=503, code="identity_validation_unavailable")


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
    identity_verified: bool = True
    session_hash: str = ""
    csrf_token: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_read_only(self) -> bool:
        return self.api_key_type == "service_readonly"

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "auth_type": self.auth_type,
            "api_key_id": self.api_key_id,
            "api_key_type": self.api_key_type,
            "identity_verified": self.identity_verified,
        }


class AuthService:
    """Manage users, sessions, API keys, and EVE identity decisions."""

    def __init__(
        self,
        repository: AuthRepository,
        resolver: Any,
        enforce_requests: bool = True,
        esi_sso_client: Any | None = None,
    ) -> None:
        self.repository = repository
        self.resolver = resolver
        self.enforce_requests = bool(enforce_requests)
        self.esi_sso_client = esi_sso_client
        self._login_failures: dict[str, list[float]] = {}
        self._login_lock = threading.Lock()
        self._esi_login_states: dict[str, dict[str, Any]] = {}
        self._esi_login_lock = threading.Lock()
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
        throttle_key = f"{remote_key}:{username_key}"
        self._check_login_rate(throttle_key)
        user = self.repository.user_by_username(username_key)
        if user is None or not _verify_password(password, str(user["password_hash"])):
            self._record_login_failure(throttle_key)
            raise AuthError("invalid username or password", 401, "invalid_credentials")
        if str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        if str(user.get("role")) != "admin":
            raise AuthError(
                "non-administrator users must sign in with EVE Online",
                403,
                "eve_sso_required",
            )
        self._clear_login_failures(throttle_key)
        return self._create_browser_session(user, "password")

    def begin_esi_login(self, return_to: str = "/") -> str:
        """Create a one-time EVE SSO authorization URL for a member login."""
        if self.esi_sso_client is None:
            raise AuthError("EVE Online login is not configured", 503, "esi_login_unavailable")
        safe_return_to = _safe_return_path(return_to)
        session = self.esi_sso_client.create_authorization_session(scopes=[])
        now = time.monotonic()
        with self._esi_login_lock:
            self._esi_login_states = {
                key: value
                for key, value in self._esi_login_states.items()
                if float(value["expires_at"]) > now
            }
            self._esi_login_states[_secret_hash(session.state)] = {
                "session": session,
                "return_to": safe_return_to,
                "expires_at": now + ESI_LOGIN_STATE_SECONDS,
            }
        return str(session.authorization_url)

    def complete_esi_login(self, callback_url: str) -> dict[str, Any]:
        """Exchange an EVE callback and create a browser session for its member."""
        query = parse_qs(urlparse(callback_url).query)
        state = str((query.get("state") or [""])[0])
        if not state:
            raise AuthError("EVE login state is missing", 400, "invalid_esi_state")
        with self._esi_login_lock:
            pending = self._esi_login_states.pop(_secret_hash(state), None)
        if pending is None:
            raise AuthError("EVE login state is invalid or already used", 400, "invalid_esi_state")
        if float(pending["expires_at"]) <= time.monotonic():
            raise AuthError("EVE login state has expired", 400, "expired_esi_state")
        try:
            code = self.esi_sso_client.parse_callback_url(
                pending["session"], callback_url
            )
            tokens = self.esi_sso_client.exchange_code(code, pending["session"])
        except EsiSsoError as exc:
            raise IdentityUnavailableError(f"EVE login failed: {exc}") from exc
        character_id = getattr(tokens, "character_id", None)
        if character_id is None:
            raise AuthError(
                "EVE login did not identify a character",
                403,
                "eve_character_missing",
            )
        try:
            profile = self.resolver.character_profile(int(character_id))
        except Exception as exc:
            raise IdentityUnavailableError(f"EVE identity lookup failed: {exc}") from exc
        corporation_id = profile.get("corporation_id")
        if corporation_id in {None, ""}:
            raise IdentityUnavailableError("EVE character corporation could not be resolved")
        corporation_id = int(corporation_id)
        if corporation_id not in self.repository.allowed_corporation_ids():
            raise AuthError(
                "this EVE character is not in an allowed corporation",
                403,
                "eve_corporation_not_allowed",
            )
        matches = self.repository.users_for_character_id(int(character_id))
        if len(matches) != 1:
            if matches:
                raise AuthError(
                    "this EVE character is assigned to multiple platform users",
                    409,
                    "eve_character_ambiguous",
                )
            username = f"eve-{int(character_id)}"
            existing = self.repository.user_by_username(_username_key(username))
            if existing is not None and str(existing.get("role")) != "member":
                username = f"eve-member-{int(character_id)}"
                existing = self.repository.user_by_username(_username_key(username))
            user = existing or self.create_user(
                username=username,
                password="",
                display_name=str(profile.get("name") or username),
                role="member",
                must_change_password=False,
                actor_user_id="eve_sso",
            )
        else:
            user = matches[0]
        if str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        now = _now_iso()
        previous = {
            int(item["character_id"]): item
            for item in self.repository.list_verified_characters(str(user["user_id"]))
        }.get(int(character_id))
        self.repository.upsert_verified_character({
            "user_id": str(user["user_id"]),
            "character_id": int(character_id),
            "character_name": str(profile.get("name") or f"EVE {int(character_id)}"),
            "corporation_id": corporation_id,
            "corporation_name": str(profile.get("corporation_name") or ""),
            "first_seen_at": str(previous.get("first_seen_at") if previous else now),
            "last_seen_at": now,
        })
        login = self._create_browser_session(
            user,
            "eve_sso",
            {
                "character_id": int(character_id),
                "character_name": str(profile.get("name") or ""),
            },
        )
        login["return_to"] = str(pending["return_to"])
        return login

    def owns_esi_login_callback(self, callback_url: str) -> bool:
        """Return whether a callback state belongs to a pending member login."""
        query = parse_qs(urlparse(callback_url).query)
        state = str((query.get("state") or [""])[0])
        if not state:
            return False
        with self._esi_login_lock:
            return _secret_hash(state) in self._esi_login_states

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
        allow_unverified: bool = True,
    ) -> AuthPrincipal:
        key = self.repository.api_key_by_hash(_secret_hash(secret))
        if key is None or str(key.get("status")) != "active":
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        user = self.repository.user_by_id(str(key["user_id"]))
        if user is None or str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        key_type = str(key.get("key_type") or "desktop")
        verified = bool(key.get("identity_verified")) or key_type == "service_readonly"
        if not verified and not allow_unverified:
            raise AuthError(
                "EVE character validation is required",
                428,
                "identity_validation_required",
            )
        self.repository.mark_api_key_used(str(key["key_id"]), _now_iso())
        return AuthPrincipal(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role=str(user["role"]),
            auth_type="api_key",
            api_key_id=str(key["key_id"]),
            api_key_type=key_type,
            identity_verified=verified,
        )

    def is_principal_active(self, principal: AuthPrincipal) -> bool:
        """Return whether an already-authenticated SSE principal remains valid."""
        user = self.repository.user_by_id(principal.user_id)
        if user is None or str(user.get("status")) != "active":
            return False
        if principal.auth_type == "api_key":
            key = self.repository.api_key_by_id(principal.api_key_id)
            return bool(key and str(key.get("status")) == "active")
        if principal.auth_type == "session":
            session = self.repository.session_by_hash(principal.session_hash)
            return bool(session and str(session.get("expires_at")) > _now_iso())
        return False

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
            "identity_verified": key_type == "service_readonly",
            "created_at": now,
            "last_used_at": "",
            "revoked_at": "",
            "revoked_reason": "",
        }
        key = self.repository.create_api_key(record)
        self._audit(actor_user_id, user_id, "api_key.created", {
            "key_id": record["key_id"], "key_type": key_type, "name": record["name"],
        })
        return {**_public_api_key(key), "secret": secret}

    def revoke_api_key(self, key_id: str, principal: AuthPrincipal) -> None:
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
        return [_public_api_key(item) for item in self.repository.list_api_keys(user_id)]

    def verify_characters(
        self,
        principal: AuthPrincipal,
        names: list[str],
    ) -> dict[str, Any]:
        if principal.auth_type != "api_key" or principal.api_key_type != "desktop":
            raise AuthError("desktop API key is required", 403, "desktop_key_required")
        clean_names = _clean_names(names)
        if not clean_names:
            raise AuthError("at least one EVE Listener is required", 428, "eve_listener_required")
        key = self.repository.api_key_by_id(principal.api_key_id) or {}
        key_details = {
            "api_key_id": principal.api_key_id,
            "api_key_name": str(key.get("name") or ""),
            "api_key_prefix": str(key.get("key_prefix") or ""),
        }
        try:
            resolved = [self._resolve_character(name) for name in clean_names]
        except AuthError as exc:
            self._audit(
                principal.user_id,
                principal.user_id,
                "identity.check_failed",
                {
                    **key_details,
                    "characters": clean_names,
                    "error_code": exc.code,
                    "reason": str(exc),
                },
            )
            raise
        allowed_corps = self.repository.allowed_corporation_ids()
        whitelisted = self.repository.whitelist_ids(principal.user_id)
        unauthorized = [
            item for item in resolved
            if item["character_id"] not in whitelisted
            and item.get("corporation_id") not in allowed_corps
        ]
        if unauthorized:
            reason = "unauthorized EVE character detected"
            now = _now_iso()
            self.repository.revoke_api_key_and_audit(
                principal.api_key_id,
                now,
                reason,
                self._audit_record(
                    principal.user_id,
                    principal.user_id,
                    "identity.key_revoked",
                    {
                        **key_details,
                        "characters": unauthorized,
                        "error_code": "unauthorized_eve_character",
                        "reason": reason,
                    },
                    now=now,
                ),
            )
            self._notify_authorization_changed()
            raise AuthError(reason, 403, "unauthorized_eve_character")
        now = _now_iso()
        existing = {
            int(item["character_id"]): item
            for item in self.repository.list_verified_characters(principal.user_id)
        }
        for item in resolved:
            previous = existing.get(int(item["character_id"]))
            self.repository.upsert_verified_character({
                "user_id": principal.user_id,
                **item,
                "first_seen_at": str(previous.get("first_seen_at") if previous else now),
                "last_seen_at": now,
            })
        self.repository.mark_api_key_verified(principal.api_key_id)
        self._audit(principal.user_id, principal.user_id, "identity.verified", {
            **key_details,
            "characters": resolved,
        })
        return {
            "verified": True,
            "permanent": True,
            "characters": resolved,
        }

    def list_users(self) -> list[dict[str, Any]]:
        users = []
        for item in self.repository.list_users():
            user = _public_user(item)
            user["keys"] = self.list_api_keys(str(item["user_id"]))
            user["whitelist"] = self.repository.list_whitelist(str(item["user_id"]))
            user["verified_characters"] = self.repository.list_verified_characters(str(item["user_id"]))
            users.append(user)
        return users

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
        """Delete a user and all owned authentication and EVE identity records."""
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

    def add_allowed_corporation(
        self,
        corporation_id: int,
        actor_user_id: str,
    ) -> dict[str, Any]:
        corporation_id = _positive_int(corporation_id, "corporation_id")
        name = ""
        try:
            profile = self.resolver.corporation_profile(corporation_id)
            name = str(profile.get("name") or "")
        except Exception as exc:
            raise IdentityUnavailableError(f"could not verify corporation: {exc}") from exc
        record = {"corporation_id": corporation_id, "corporation_name": name, "created_at": _now_iso()}
        self.repository.upsert_allowed_corporation(record)
        self._audit(actor_user_id, "", "corporation.allowed", record)
        return record

    def delete_allowed_corporation(self, corporation_id: int, actor_user_id: str) -> None:
        corporation_id = _positive_int(corporation_id, "corporation_id")
        self.repository.delete_allowed_corporation(corporation_id)
        self._audit(actor_user_id, "", "corporation.removed", {"corporation_id": corporation_id})
        self.reevaluate_all_users(actor_user_id)

    def add_whitelist_character(
        self,
        user_id: str,
        character_id: int,
        note: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        if self.repository.user_by_id(user_id) is None:
            raise AuthError("user not found", 404, "user_not_found")
        character_id = _positive_int(character_id, "character_id")
        try:
            profile = self.resolver.character_profile(character_id)
        except Exception as exc:
            raise IdentityUnavailableError(f"could not verify character: {exc}") from exc
        record = {
            "user_id": user_id,
            "character_id": character_id,
            "character_name": str(profile.get("name") or ""),
            "note": str(note or "").strip()[:500],
            "created_at": _now_iso(),
        }
        self.repository.upsert_whitelist(record)
        self._audit(actor_user_id, user_id, "character.whitelisted", record)
        return record

    def delete_whitelist_character(
        self,
        user_id: str,
        character_id: int,
        actor_user_id: str,
    ) -> None:
        character_id = _positive_int(character_id, "character_id")
        self.repository.delete_whitelist(user_id, character_id)
        self._audit(actor_user_id, user_id, "character.whitelist_removed", {"character_id": character_id})
        self.reevaluate_user(user_id, actor_user_id)

    def reevaluate_all_users(self, actor_user_id: str) -> None:
        for user in self.repository.list_users():
            if str(user.get("status")) == "active":
                self.reevaluate_user(str(user["user_id"]), actor_user_id)

    def reevaluate_user(self, user_id: str, actor_user_id: str) -> None:
        allowed_corps = self.repository.allowed_corporation_ids()
        whitelisted = self.repository.whitelist_ids(user_id)
        unauthorized = [
            item for item in self.repository.list_verified_characters(user_id)
            if int(item["character_id"]) not in whitelisted
            and item.get("corporation_id") not in allowed_corps
        ]
        if not unauthorized:
            return
        now = _now_iso()
        self.repository.revoke_desktop_keys_and_audit(
            user_id,
            now,
            "authorization rules no longer allow a verified EVE character",
            self._audit_record(
                actor_user_id,
                user_id,
                "identity.desktop_keys_revoked",
                {"characters": unauthorized},
                now=now,
            ),
        )
        self._notify_authorization_changed()

    def _resolve_character(self, name: str) -> dict[str, Any]:
        try:
            resolved = self.resolver.resolve_names([name])
            exact = next(
                (
                    item for item in resolved
                    if str(getattr(item, "category", "")).casefold() == "character"
                    and str(getattr(item, "name", "")).casefold() == name.casefold()
                ),
                None,
            )
            if exact is None:
                raise IdentityUnavailableError(f"EVE character could not be resolved: {name}")
            profile = self.resolver.character_profile(int(exact.entity_id))
        except IdentityUnavailableError:
            raise
        except Exception as exc:
            raise IdentityUnavailableError(f"EVE identity lookup failed for {name}: {exc}") from exc
        corporation_id = profile.get("corporation_id")
        return {
            "character_id": int(exact.entity_id),
            "character_name": str(profile.get("name") or exact.name),
            "corporation_id": int(corporation_id) if corporation_id not in {None, ""} else None,
            "corporation_name": str(profile.get("corporation_name") or ""),
        }

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

    def _check_login_rate(self, key: str) -> None:
        import time

        cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
        with self._login_lock:
            failures = [item for item in self._login_failures.get(key, []) if item >= cutoff]
            self._login_failures[key] = failures
            if len(failures) >= LOGIN_FAILURE_LIMIT:
                raise AuthError("too many login attempts", 429, "login_rate_limited")

    def _record_login_failure(self, key: str) -> None:
        import time

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
            "identity_verified", "created_at", "last_used_at", "revoked_at", "revoked_reason",
        )
    }


def _clean_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AuthError(f"{label} must be a positive integer", 400, f"invalid_{label}") from exc
    if number <= 0:
        raise AuthError(f"{label} must be a positive integer", 400, f"invalid_{label}")
    return number


def _username_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _safe_return_path(value: str) -> str:
    path = str(value or "/").strip()
    parsed = urlparse(path)
    if not path.startswith("/") or path.startswith("//") or parsed.scheme or parsed.netloc:
        return "/"
    return path


def _secret_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
