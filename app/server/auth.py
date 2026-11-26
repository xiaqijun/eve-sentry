"""Authentication and EVE character authorization services."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.server.auth_store import AuthRepository
from app.server.seat_token_vault import SeatTokenVault


SESSION_COOKIE_NAME = "eve_sentry_session"
SESSION_HOURS = 12
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_IP_FAILURE_LIMIT = 25
LOCAL_CLIENT_KEY_TYPE = "local_client"
SEAT_RUNTIME_SETTING = "seat_runtime"
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
    seat_enabled_at: str = ""

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


@dataclass(frozen=True)
class SeatRuntimeSettings:
    """Persisted browser override for the startup edition."""

    enabled: bool | None
    revision: int
    enabled_at: str
    service_token_hash: str = ""


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
        local_client_keys_enabled: bool = False,
        seat_token_vault: SeatTokenVault | None = None,
    ) -> None:
        self.repository = repository
        self.resolver = resolver
        self.enforce_requests = bool(enforce_requests)
        normalized_seat_mode = str(seat_auth_mode or "off").strip().casefold()
        if normalized_seat_mode not in {"off", "enforce"}:
            raise ValueError("seat_auth_mode must be off or enforce")
        if local_client_keys_enabled and (not self.enforce_requests or normalized_seat_mode != "off"):
            raise ValueError("local client keys require standalone enforced authentication")
        self.seat_auth_mode = normalized_seat_mode
        self._startup_local_keys = local_client_keys_enabled
        self._seat_runtime_lock = threading.Lock()
        self._seat_runtime: SeatRuntimeSettings | None = None
        self._seat_last_request: tuple[SeatRuntimeSettings | None, str] | None = None
        self._seat_token_vault = seat_token_vault
        self._seat_copy_token = ""
        if self.enforce_requests:
            saved = self.repository.setting(SEAT_RUNTIME_SETTING)
            if saved is not None:
                self._seat_runtime = SeatRuntimeSettings(**json.loads(saved))
                self._restore_seat_copy_token(self._seat_runtime.service_token_hash)
        self._login_failures: dict[str, list[float]] = {}
        self._login_lock = threading.Lock()
        self._authorization_generation = 0
        self._authorization_change_lock = threading.Lock()
        self._authorization_change_listeners: set[Callable[[], None]] = set()

    @property
    def local_client_keys_enabled(self) -> bool:
        runtime = self._seat_runtime
        return not runtime.enabled if runtime is not None and runtime.enabled is not None else self._startup_local_keys

    def seat_integration_status(self, startup_token: str) -> dict[str, Any]:
        with self._seat_runtime_lock:
            return self._seat_integration_status(startup_token)

    def _seat_integration_status(self, startup_token: str) -> dict[str, Any]:
        runtime = self._seat_runtime
        override = runtime.enabled if runtime is not None else None
        token_hash = runtime.service_token_hash if runtime is not None else ""
        startup_available = len(startup_token) >= 32 and (
            not token_hash or hmac.compare_digest(_secret_hash(startup_token), token_hash)
        )
        last_request = self._seat_last_request
        return {
            "enabled": override if override is not None else self.seat_auth_mode == "enforce",
            "local_issuance": self.local_client_keys_enabled,
            "revision": runtime.revision if runtime is not None else 0,
            "token_configured": bool(token_hash) or len(startup_token) >= 32,
            "token_copy_available": self.enforce_requests and bool(self._seat_copy_token or startup_available),
            "editable": self.enforce_requests,
            "last_authenticated_at": (
                last_request[1] if last_request is not None and last_request[0] is runtime else None
            ),
        }

    def apply_seat_integration_settings(
        self, payload: dict[str, Any], startup_token: str,
    ) -> tuple[dict[str, Any], str]:
        if not self.enforce_requests:
            raise AuthError("请先启用强制鉴权", 409, "seat_requires_auth")
        enabled = payload.get("enabled")
        revision = payload.get("revision")
        supplied = payload.get("service_token", "")
        generate = payload.get("generate_service_token", False)
        return_service_token = payload.get("return_service_token", True)
        if (
            ("enabled" in payload and type(enabled) is not bool)
            or type(revision) is not int or revision < 0
            or type(generate) is not bool or type(return_service_token) is not bool or not isinstance(supplied, str)
            or (generate and supplied) or (enabled is None and not generate and not supplied)
        ):
            raise AuthError("请提交有效的启用状态、服务令牌和配置版本", 400, "invalid_seat_settings")
        if supplied and (not 32 <= len(supplied) <= 512 or any(not 33 <= ord(ch) <= 126 for ch in supplied)):
            raise AuthError("服务令牌须为 32～512 个不含空白的 ASCII 字符", 400, "invalid_seat_token")
        if generate and not return_service_token and self._seat_token_vault is None:
            raise AuthError("无法保存可复制的令牌", 503, "seat_token_save_failed")
        generated = secrets.token_hex(32) if generate else ""
        replacement = generated or supplied
        replacement_hash = _secret_hash(replacement) if replacement else ""
        auth_changed = False
        with self._seat_runtime_lock:
            current = self._seat_runtime or SeatRuntimeSettings(None, 0, "")
            if revision != current.revision:
                raise AuthError("设置已被修改，请刷新后重试", 409, "seat_settings_conflict")
            token_hash = replacement_hash or current.service_token_hash
            if enabled and not token_hash and len(startup_token) < 32:
                raise AuthError("请先生成或填入 SeAT 服务令牌", 409, "seat_token_required")
            override = enabled if enabled is not None else current.enabled
            settings_to_save: dict[str, str] = {}
            now = _now_iso()
            if override != current.enabled or token_hash != current.service_token_hash:
                enabled_before = current.enabled if current.enabled is not None else self.seat_auth_mode == "enforce"
                enabled_at = current.enabled_at
                if enabled is not None and enabled != enabled_before:
                    enabled_at = now if enabled else ""
                updated = SeatRuntimeSettings(override, revision + 1, enabled_at, token_hash)
                settings_to_save[SEAT_RUNTIME_SETTING] = json.dumps({
                    "enabled": updated.enabled,
                    "revision": updated.revision,
                    "enabled_at": updated.enabled_at,
                    "service_token_hash": updated.service_token_hash,
                })
            if replacement and self._seat_token_vault is not None:
                try:
                    self._seat_token_vault.save(replacement, now, settings=settings_to_save)
                except Exception as exc:
                    logger.warning("SeAT token update could not be saved (%s)", type(exc).__name__)
                    raise AuthError("令牌保存失败，当前令牌保持原值", 503, "seat_token_save_failed") from None
            elif settings_to_save:
                self.repository.set_settings(settings_to_save, now)
            if settings_to_save:
                self._seat_runtime = updated
                auth_changed = override != current.enabled
            if replacement:
                self._seat_copy_token = replacement
            settings = self._seat_integration_status(startup_token)
        if auth_changed:
            self._notify_authorization_changed()
        return settings, generated if return_service_token else ""

    def authenticate_seat_service_token(self, provided: str, startup_token: str) -> None:
        with self._seat_runtime_lock:
            runtime = self._seat_runtime
            token_hash = runtime.service_token_hash if runtime is not None else ""
            web_token_while_off = bool(token_hash and runtime.enabled is None and self.seat_auth_mode == "off")
            if self.local_client_keys_enabled or web_token_while_off or (not token_hash and not startup_token):
                raise AuthError("Seat integration is disabled", 503, "seat_integration_disabled")
            expected = token_hash or _secret_hash(startup_token)
            if not provided or not hmac.compare_digest(_secret_hash(provided), expected):
                raise AuthError("Seat integration service token is invalid", 401, "seat_integration_unauthorized")
            self._seat_last_request = (runtime, _now_iso())
            if token_hash:
                self._remember_seat_copy_token(provided)

    def copy_seat_service_token(self, startup_token: str, actor_user_id: str) -> str:
        if not self.enforce_requests:
            raise AuthError("请先启用强制鉴权", 409, "seat_requires_auth")
        with self._seat_runtime_lock:
            runtime = self._seat_runtime
            token_hash = runtime.service_token_hash if runtime is not None else ""
            if token_hash:
                token = next(
                    (value for value in (self._seat_copy_token, startup_token)
                     if value and hmac.compare_digest(_secret_hash(value), token_hash)),
                    "",
                )
                if not token:
                    raise AuthError("等待 SeAT 成功鉴权后再复制", 409, "seat_token_copy_pending")
            else:
                token = startup_token
                if len(token) < 32:
                    raise AuthError("服务令牌尚未配置", 409, "seat_token_required")
            self._audit(actor_user_id, "", "seat.integration.token_copy", {})
            return token

    def _restore_seat_copy_token(self, token_hash: str) -> None:
        if not token_hash or self._seat_token_vault is None:
            return
        try:
            token = self._seat_token_vault.load()
        except Exception as exc:
            logger.warning("SeAT token copy could not be restored (%s)", type(exc).__name__)
            return
        if token and hmac.compare_digest(_secret_hash(token), token_hash):
            self._seat_copy_token = token

    def _remember_seat_copy_token(self, token: str) -> None:
        if self._seat_copy_token == token:
            return
        self._seat_copy_token = token
        if self._seat_token_vault is not None:
            try:
                self._seat_token_vault.save(token, _now_iso())
            except Exception as exc:
                # Copy availability must not affect an already valid SeAT request.
                logger.warning("SeAT token copy could not be persisted (%s)", type(exc).__name__)

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
        runtime = self._seat_runtime
        override = runtime.enabled if runtime is not None else None
        seat_enabled = override if override is not None else self.seat_auth_mode == "enforce"
        local_keys = not override if override is not None else self._startup_local_keys
        secret_hash = _secret_hash(secret)
        key = self.repository.api_key_by_hash(secret_hash)
        if key is None:
            if seat_enabled:
                return self._authenticate_seat_api_key(
                    secret_hash, runtime.enabled_at if runtime is not None else "",
                )
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        if str(key.get("status")) != "active":
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        key_type = str(key.get("key_type") or "desktop")
        is_local_client = key_type == LOCAL_CLIENT_KEY_TYPE
        if is_local_client != local_keys:
            raise AuthError("API key is invalid for this server edition", 401, "invalid_api_key")
        user = self.repository.user_by_id(str(key["user_id"]))
        if user is None or str(user.get("status")) != "active":
            raise AuthError("user is disabled", 403, "user_disabled")
        self.repository.mark_api_key_used(str(key["key_id"]), _now_iso())
        return AuthPrincipal(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role="client" if is_local_client else str(user["role"]),
            auth_type="api_key",
            api_key_id=str(key["key_id"]),
            api_key_type=key_type,
            permissions=("monitor", "alert") if is_local_client else (),
        )

    def _authenticate_seat_api_key(self, secret_hash: str, enabled_at: str) -> AuthPrincipal:
        key = self.repository.seat_integration_key_by_hash(secret_hash)
        if key is None:
            raise AuthError("API key is invalid or revoked", 401, "invalid_api_key")
        key_id = str(key.get("key_id") or "")
        account_id = str(key.get("account_id") or "")
        if str(key.get("status") or "") != "active":
            raise AuthError("Seat API key is revoked", 401, "invalid_api_key")
        if not account_id:
            raise AuthError("Seat API key is malformed", 401, "invalid_api_key")

        # Seat owns the client identity. The warning platform has no separate
        # end-user account for a Seat client, so local user/binding rows must
        # not gate authentication. Keep a stable namespaced ID for attribution.
        principal_id = f"seat:{account_id}"
        principal = AuthPrincipal(
            user_id=principal_id,
            username=principal_id,
            display_name=principal_id,
            role="client",
            auth_type="api_key",
            api_key_id=key_id,
            api_key_type="seat",
            integration="seat",
            account_id=account_id,
            seat_enabled_at=enabled_at,
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
        if principal.is_seat:
            runtime = self._seat_runtime
            override = runtime.enabled if runtime is not None else None
            if override is False or (override is None and self.seat_auth_mode == "off"):
                return False
            if runtime is not None and principal.seat_enabled_at != runtime.enabled_at:
                return False
            key = self.repository.seat_integration_key_by_id(principal.api_key_id)
            return bool(
                key
                and str(key.get("status") or "") == "active"
                and str(key.get("account_id") or "") == principal.account_id
            )
        user = self.repository.user_by_id(principal.user_id)
        if user is None or str(user.get("status")) != "active":
            return False
        if principal.auth_type == "api_key":
            if (principal.api_key_type == LOCAL_CLIENT_KEY_TYPE) != self.local_client_keys_enabled:
                return False
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
        """Record a legacy Seat-to-local-user association for migration only."""
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
        """Disable or enable a legacy Seat binding record."""
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
        key_types = {LOCAL_CLIENT_KEY_TYPE} if self.local_client_keys_enabled else {"desktop", "service_readonly"}
        if key_type not in key_types:
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
        audit = self._audit_record(actor_user_id, user_id, "api_key.created", {
            "key_id": record["key_id"], "key_type": key_type, "name": record["name"],
        }, now)
        key = self.repository.create_api_key(record, audit)
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
        for item in self.repository.list_seat_integration_keys():
            user_id = str(item.get("user_id") or "").strip()
            if not user_id:
                continue
            keys_by_user.setdefault(user_id, []).append(
                self._public_seat_api_key_record(item)
            )
        users = []
        for item in user_rows:
            user = _public_user(item)
            user["keys"] = keys_by_user.get(str(item.get("user_id") or ""), [])
            users.append(user)
        local_user_ids = {str(item.get("user_id") or "") for item in user_rows}
        for user_id, key_rows_for_user in keys_by_user.items():
            if user_id in local_user_ids or not user_id.startswith("seat:"):
                continue
            users.append(
                {
                    "user_id": user_id,
                    "username": user_id,
                    "display_name": user_id,
                    "role": "client",
                    "status": "active",
                    "must_change_password": False,
                    "disabled_reason": "",
                    "created_at": "",
                    "updated_at": "",
                    "keys": key_rows_for_user,
                }
            )
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
