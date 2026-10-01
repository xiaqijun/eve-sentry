"""HTTP authentication middleware and account-management routes."""

from __future__ import annotations

import json
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
from ipaddress import ip_address
from typing import Any
from urllib.parse import unquote

from app.server.auth import AuthError, AuthPrincipal, AuthService, SESSION_COOKIE_NAME


_PUBLIC_USER_FIELDS = (
    "user_id",
    "username",
    "display_name",
    "role",
    "status",
)
_PUBLIC_KEY_FIELDS = (
    "key_id",
    "user_id",
    "name",
    "key_prefix",
    "key_type",
    "status",
    "created_at",
    "last_used_at",
    "revoked_at",
    "revoked_reason",
)
_USAGE_CLIENT_FIELDS = (
    "client_id",
    "client_type",
    "label",
    "status",
    "online",
    "seen_at",
    "remote_ip",
)


_SEAT_PERMISSION_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "monitor": (
        ("GET", "/api/v1/bootstrap"),
        ("GET", "/api/v1/map"),
        ("GET", "/api/v1/map/neighborhood"),
        ("GET", "/api/v1/clients"),
        ("GET", "/api/v1/systems"),
        ("GET", "/api/v1/systems/*"),
        ("GET", "/api/v1/map/systems/*"),
        ("GET", "/api/v1/characters/*"),
        ("GET", "/api/v1/esi/status"),
        ("GET", "/api/heartbeats"),
        ("GET", "/api/esi/status"),
        ("GET", "/api/intel"),
        ("GET", "/api/systems"),
        ("GET", "/api/systems/*"),
        ("GET", "/api/characters/*"),
        ("GET", "/api/intel/*"),
        ("POST", "/api/v1/channel-lines"),
        ("POST", "/api/v1/clients/heartbeats"),
        # Compatibility for pre-identity-retirement clients.  These requests
        # are accepted as a no-op; no EVE identity or ESI lookup is performed.
        ("POST", "/api/v1/client/identity-check"),
        ("POST", "/api/v1/client/identity-checks"),
        ("POST", "/api/heartbeats"),
        ("POST", "/api/channel-lines"),
        ("POST", "/api/intel"),
        ("POST", "/api/observations"),
        ("POST", "/api/v1/hostile-presence"),
        ("POST", "/api/v1/ocr/query"),
        ("POST", "/api/v1/ocr/snapshot"),
        ("POST", "/api/v1/reports"),
        ("POST", "/api/v1/observations"),
    ),
    "alert": (
        ("GET", "/api/v1/bootstrap"),
        ("GET", "/api/v1/map"),
        ("GET", "/api/v1/map/neighborhood"),
        ("GET", "/api/v1/active-intel"),
        ("GET", "/api/v1/alert-history"),
        ("GET", "/api/v1/hostile-waves"),
        ("GET", "/api/v1/integrations/hostile-systems"),
        ("GET", "/api/v1/events"),
        ("GET", "/api/v1/alerts"),
        ("GET", "/api/v1/alerts/*"),
        ("GET", "/api/v1/clients"),
        ("POST", "/api/v1/alert-deliveries/*/ack"),
    ),
}


_LEGACY_CLIENT_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/heartbeats"),
    ("GET", "/api/esi/status"),
    ("GET", "/api/intel"),
    ("GET", "/api/systems"),
    ("GET", "/api/systems/*"),
    ("GET", "/api/characters/*"),
    ("GET", "/api/intel/*"),
    ("GET", "/api/reports"),
    ("GET", "/api/observations"),
    ("GET", "/api/alerts"),
    ("GET", "/api/alerts/*"),
    ("GET", "/api/events"),
    ("POST", "/api/heartbeats"),
    ("POST", "/api/channel-lines"),
    ("POST", "/api/intel"),
    ("POST", "/api/observations"),
)

# The QQ bot runs on the same host as the warning service.  Keep its
# credential-free path strictly loopback-only; never turn the public SSE or
# map routes into anonymous endpoints.
_LOCAL_BOT_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/bootstrap"),
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/ocr/query/*"),
    ("POST", "/api/v1/ocr/query"),
)
_LOCAL_BOT_HEADER = "X-EVE-SENTRY-Embedded-Bot"


def build_admin_clients_payload(
    client_snapshot: dict[str, Any],
    users: list[dict[str, Any]],
) -> dict[str, Any]:
    """Enrich private heartbeat attribution and aggregate API-key usage.

    Keep every currently online instance, but collapse offline history for the
    same user/type/host tuple to its newest record. This removes reinstall and
    upgrade residue without hiding genuinely concurrent clients.
    """
    owners: dict[str, dict[str, Any]] = {}
    keys: dict[str, dict[str, Any]] = {}
    ordered_keys: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for user in users:
        owner = {field: user.get(field) for field in _PUBLIC_USER_FIELDS}
        user_id = str(owner.get("user_id") or "").strip()
        if user_id:
            owners[user_id] = owner
        user_keys = user.get("keys")
        if not isinstance(user_keys, list):
            continue
        for value in user_keys:
            if not isinstance(value, dict):
                continue
            key = {field: value.get(field) for field in _PUBLIC_KEY_FIELDS}
            key_id = str(key.get("key_id") or "").strip()
            if not key_id:
                continue
            keys[key_id] = key
            ordered_keys.append((owner, key))

    raw_heartbeats = client_snapshot.get("heartbeats")
    if not isinstance(raw_heartbeats, list):
        raw_heartbeats = []
    heartbeats: list[dict[str, Any]] = []
    for value in raw_heartbeats:
        if not isinstance(value, dict):
            continue
        heartbeat = dict(value)
        user_id = str(heartbeat.get("user_id") or "").strip()
        key_id = str(heartbeat.get("api_key_id") or "").strip()
        heartbeat["owner"] = owners.get(user_id)
        heartbeat["key"] = keys.get(key_id)
        heartbeats.append(heartbeat)
    heartbeats.sort(key=lambda item: str(item.get("seen_at") or ""), reverse=True)

    deduplicated: list[dict[str, Any]] = []
    seen_logical_clients: set[tuple[str, str, str]] = set()
    online_logical_clients: set[tuple[str, str, str]] = set()
    hidden_duplicate_count = 0
    for heartbeat in heartbeats:
        details = heartbeat.get("details")
        details = details if isinstance(details, dict) else {}
        owner_id = str(heartbeat.get("user_id") or "").strip()
        client_type = str(heartbeat.get("client_type") or "client").strip()
        host = str(details.get("host") or "").strip().casefold()
        if owner_id and host and bool(heartbeat.get("online")):
            online_logical_clients.add((owner_id, client_type, host))

    for heartbeat in heartbeats:
        details = heartbeat.get("details")
        details = details if isinstance(details, dict) else {}
        owner_id = str(heartbeat.get("user_id") or "").strip()
        client_type = str(heartbeat.get("client_type") or "client").strip()
        host = str(details.get("host") or "").strip().casefold()
        # Without an owner and host there is no safe way to infer that two
        # records belong to one installation; retain both records.
        if not owner_id or not host:
            deduplicated.append(heartbeat)
            continue
        logical_key = (owner_id, client_type, host)
        if bool(heartbeat.get("online")):
            deduplicated.append(heartbeat)
            seen_logical_clients.add(logical_key)
            continue
        if logical_key in online_logical_clients:
            hidden_duplicate_count += 1
            continue
        if logical_key in seen_logical_clients:
            hidden_duplicate_count += 1
            continue
        seen_logical_clients.add(logical_key)
        deduplicated.append(heartbeat)
    heartbeats = deduplicated

    heartbeats_by_key: dict[str, list[dict[str, Any]]] = {}
    for heartbeat in heartbeats:
        key_id = str(heartbeat.get("api_key_id") or "").strip()
        if key_id:
            heartbeats_by_key.setdefault(key_id, []).append(heartbeat)

    summary = dict(client_snapshot.get("summary") or {})
    summary.pop("items", None)
    clients = {
        **client_snapshot,
        "heartbeats": heartbeats,
        "count": len(heartbeats),
        "summary": {**summary, "hidden_duplicate_count": hidden_duplicate_count},
    }

    usage_records: list[dict[str, Any]] = []
    for owner, key in ordered_keys:
        key_id = str(key.get("key_id") or "").strip()
        linked = heartbeats_by_key.get(key_id, [])
        linked_clients = [
            {field: heartbeat.get(field) for field in _USAGE_CLIENT_FIELDS}
            for heartbeat in linked
        ]
        last_ip = next(
            (
                str(heartbeat.get("remote_ip") or "").strip()
                for heartbeat in linked
                if str(heartbeat.get("remote_ip") or "").strip()
            ),
            "",
        )
        usage_records.append(
            {
                "owner": owner,
                "key": key,
                "linked_clients": linked_clients,
                "client_count": len(linked_clients),
                "online_count": sum(
                    1 for heartbeat in linked if bool(heartbeat.get("online"))
                ),
                "last_client": linked_clients[0] if linked_clients else None,
                "last_ip": last_ip,
            }
        )

    return {"clients": clients, "keys": usage_records}


def _seat_permissions_for_request(method: str, path: str) -> set[str]:
    """Return permissions accepted by one Seat principal request."""
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    accepted: set[str] = set()
    for permission, routes in _SEAT_PERMISSION_ROUTES.items():
        for route_method, route_path in routes:
            if normalized_method != route_method:
                continue
            if normalized_path == route_path:
                accepted.add(permission)
                continue
            if route_path.endswith("/*/ack") and normalized_path.startswith(route_path[:-5]) and normalized_path.endswith("/ack"):
                middle = normalized_path[len(route_path[:-5]):-4].strip("/")
                if middle and "/" not in middle:
                    accepted.add(permission)
                    continue
            if route_path.endswith("/*") and normalized_path.startswith(route_path[:-1]):
                accepted.add(permission)
    return accepted


def _is_legacy_client_route(method: str, path: str) -> bool:
    """Identify compatibility endpoints that must not accept historical keys."""
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    for route_method, route_path in _LEGACY_CLIENT_ROUTES:
        if normalized_method != route_method:
            continue
        if normalized_path == route_path:
            return True
        if route_path.endswith("/*") and normalized_path.startswith(route_path[:-1]):
            return True
    return False


class AuthHttpMixin:
    """Mixin used by the standard-library request handler."""

    _auth_principal: AuthPrincipal | None = None

    def _auth_service(self) -> AuthService | None:
        return getattr(type(self), "auth_service", None)

    def _is_local_bot_request(self, method: str, path: str) -> bool:
        """Allow only marked loopback bot requests on narrow read/query routes."""
        if str(self.headers.get(_LOCAL_BOT_HEADER) or "").strip() != "1":
            return False
        peer = str(getattr(self, "client_address", ("",))[0]).strip()
        try:
            if not ip_address(peer).is_loopback:
                return False
        except ValueError:
            return False
        normalized_method = str(method or "").upper()
        normalized_path = str(path or "")
        for route_method, route_path in _LOCAL_BOT_ROUTES:
            if normalized_method != route_method:
                continue
            if normalized_path == route_path:
                return True
            if route_path.endswith("/*") and normalized_path.startswith(route_path[:-1]):
                suffix = normalized_path[len(route_path[:-1]):].strip("/")
                if suffix and "/" not in suffix:
                    return True
        return False

    def _authorize_request(self, method: str, path: str) -> bool:
        service = self._auth_service()
        self._auth_principal = None
        if self._is_seat_integration_request(method, path):
            return self._authorize_seat_integration_request()
        if service is None:
            return True
        if path in {"/api/health", "/api/livez", "/api/readyz"}:
            return True
        if self._is_local_bot_request(method, path):
            return True
        if path == "/api/v1/auth/login" and method == "POST":
            return True
        client_route = bool(_seat_permissions_for_request(method, path))
        legacy_client_route = _is_legacy_client_route(method, path)
        if not service.enforce_requests and not self._is_auth_management_path(path):
            # Keep the historical auth=off behavior for anonymous and ordinary
            # requests. When Seat auth is enabled, inspect a supplied Bearer
            # key so Seat principals can be scoped without making all legacy
            # clients authenticate during the rollout.
            authorization = str(self.headers.get("Authorization") or "").strip()
            seat_mode = str(getattr(service, "seat_auth_mode", "off") or "off")
            if (
                not client_route
                or (
                    seat_mode != "enforce"
                    and not authorization.casefold().startswith("bearer ")
                )
            ):
                return True

        try:
            authorization = str(self.headers.get("Authorization") or "").strip()
            if authorization.casefold().startswith("bearer "):
                secret = authorization[7:].strip()
                principal = service.authenticate_api_key(secret)
            else:
                session_token = self._session_cookie()
                if not session_token:
                    raise AuthError("authentication is required", 401, "authentication_required")
                principal = service.authenticate_session(session_token)

            if client_route and principal.auth_type == "api_key" and not principal.is_seat:
                raise AuthError(
                    "Client key must be issued by SeAT",
                    403,
                    "seat_client_key_required",
                )
            if (
                legacy_client_route
                and principal.auth_type == "api_key"
                and not principal.is_seat
            ):
                raise AuthError(
                    "Client key must be issued by SeAT",
                    403,
                    "seat_client_key_required",
                )
            if principal.is_seat:
                required = _seat_permissions_for_request(method, path)
                # Older clients use auth/me as their key-validation probe.
                # Keep this read-only compatibility exception out of the
                # monitor/alert route tables so other management paths remain
                # denied for Seat keys.
                if method == "GET" and path == "/api/v1/auth/me":
                    required = {"monitor", "alert"}
                if not required or not any(
                    permission in principal.permissions for permission in required
                ):
                    raise AuthError(
                        "Seat key does not have permission for this endpoint",
                        403,
                        "seat_permission_denied",
                    )

            # The OCR query endpoint is a command request for connected
            # detector clients, but it does not mutate persisted intel data.
            # Allow the QQ service key to enqueue this bounded command while
            # keeping all other write routes read-only.
            if (
                principal.is_read_only
                and method not in {"GET", "HEAD"}
                and path != "/api/v1/ocr/query"
            ):
                raise AuthError("service key is read-only", 403, "read_only_key")
            if principal.is_read_only and path not in {
                "/api/v1/bootstrap",
                "/api/v1/events",
                "/api/v1/alert-history",
                "/api/v1/hostile-waves",
                "/api/v1/integrations/hostile-systems",
                "/api/v1/ocr/query",
            } and not path.startswith("/api/v1/ocr/query/"):
                raise AuthError(
                    "service key can only read approved integration endpoints",
                    403,
                    "service_key_scope_denied",
                )
            if path.startswith("/api/v1/admin/") and not principal.is_admin:
                raise AuthError("administrator access is required", 403, "forbidden")
            if (
                principal.auth_type == "session"
                and method not in {"GET", "HEAD"}
                and str(self.headers.get("X-CSRF-Token") or "") != principal.csrf_token
            ):
                raise AuthError("CSRF token is invalid", 403, "invalid_csrf_token")
        except AuthError as exc:
            self._send_auth_error(exc)
            return False

        self._auth_principal = principal
        return True

    def _is_seat_integration_request(self, method: str, path: str) -> bool:
        """Identify Seat integration methods before browser auth middleware."""
        create_path = "/api/v1/integrations/seat/keys"
        service_paths = {
            ("POST", create_path),
            ("POST", "/api/v1/integrations/seat/alert-grants"),
            ("POST", "/api/v1/integrations/seat/alert-events"),
            ("POST", "/api/v1/integrations/seat/alert-deliveries"),
            ("GET", "/api/v1/integrations/seat/alert-events"),
        }
        if (method, path) in service_paths:
            return True
        revoke_prefixes = (
            f"{create_path}/",
            "/api/v1/integrations/seat/alert-grants/",
        )
        if method != "DELETE":
            return False
        for prefix in revoke_prefixes:
            item_id = path[len(prefix):].strip("/") if path.startswith(prefix) else ""
            if item_id and "/" not in item_id:
                return True
        return False

    def _authorize_seat_integration_request(self) -> bool:
        """Authenticate the Seat service token without accepting a browser session."""
        configured = str(
            getattr(type(self), "seat_integration_token", "") or ""
        ).strip()
        if not configured:
            self._send_json(
                {
                    "error": "Seat integration is disabled",
                    "code": "seat_integration_disabled",
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return False
        authorization = str(self.headers.get("Authorization") or "").strip()
        provided = (
            authorization[7:].strip()
            if authorization.casefold().startswith("bearer ")
            else ""
        )
        if not provided or not hmac.compare_digest(provided, configured):
            self._send_json(
                {
                    "error": "Seat integration service token is invalid",
                    "code": "seat_integration_unauthorized",
                },
                HTTPStatus.UNAUTHORIZED,
            )
            return False
        self._seat_integration_authenticated = True
        return True

    def _is_auth_management_path(self, path: str) -> bool:
        return path.startswith((
            "/api/v1/auth/",
            "/api/v1/me/",
            "/api/v1/admin/",
        ))

    def _stream_principal_active(self) -> bool:
        service = self._auth_service()
        principal = self._auth_principal
        if service is None or principal is None:
            return True
        return service.is_principal_active(principal)

    def _handle_auth_get(self, path: str) -> bool:
        service = self._auth_service()
        if service is None:
            return False
        auth_paths = {
            "/api/v1/auth/me",
            "/api/v1/me/keys",
            "/api/v1/admin/users",
            "/api/v1/admin/clients",
            "/api/v1/admin/audit",
            "/api/v1/admin/esi-gateway",
            "/api/v1/admin/personnel-settings",
        }
        if path not in auth_paths:
            return False
        principal = self._require_principal()
        if path == "/api/v1/auth/me":
            user = service.repository.user_by_id(principal.user_id)
            payload = principal.to_dict()
            payload["must_change_password"] = bool(user.get("must_change_password")) if user else False
            payload["csrf_token"] = principal.csrf_token if principal.auth_type == "session" else ""
            self._send_json({"user": payload})
            return True
        if path == "/api/v1/me/keys":
            self._send_json({"keys": service.list_api_keys(principal.user_id)})
            return True
        if path == "/api/v1/admin/users":
            self._send_json({"users": service.list_users()})
            return True
        if path == "/api/v1/admin/personnel-settings":
            try:
                self._send_json({"settings": self._personnel_settings().snapshot()})
            except AuthError as exc:
                self._send_auth_error(exc)
            return True
        if path == "/api/v1/admin/clients":
            snapshot = self._store().management_heartbeat_snapshot()
            self._send_json(
                build_admin_clients_payload(
                    snapshot,
                    service.list_users_with_api_keys(),
                )
            )
            return True
        if path == "/api/v1/admin/audit":
            self._send_json({"audit": service.repository.list_audit()})
            return True
        if path == "/api/v1/admin/esi-gateway":
            self._send_json(self._esi_gateway_observability())
            return True
        return False

    def _handle_auth_post(self, path: str) -> bool:
        service = self._auth_service()
        if service is None:
            return False
        auth_paths = {
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/password",
            "/api/v1/me/keys",
            "/api/v1/admin/users",
            "/api/v1/admin/personnel-settings",
        }
        user_action = self._admin_user_action(path)
        key_action = self._api_key_action(path)
        if path not in auth_paths and user_action is None and key_action is None:
            return False
        try:
            if path == "/api/v1/auth/login":
                payload = self._read_json()
                login = service.login(
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                    self._login_client_ip(),
                )
                self._send_auth_json(
                    {"user": login["user"], "csrf_token": login["csrf_token"]},
                    cookie=self._session_cookie_header(login["session_token"]),
                )
                return True

            principal = self._require_principal()
            if path == "/api/v1/me/keys" or (
                user_action is not None
                and user_action[1] in {"keys", "service-keys"}
            ):
                self._send_json(
                    {
                        "error": "Client keys are issued and managed by GloryNavy_Seat",
                        "code": "seat_key_management_required",
                    },
                    HTTPStatus.GONE,
                )
                return True
            payload = self._read_optional_json()
            if path == "/api/v1/admin/personnel-settings":
                settings = self._personnel_settings().update(payload, principal.user_id)
                self._send_json({"ok": True, "settings": settings})
                return True
            if path == "/api/v1/auth/logout":
                service.logout(principal)
                self._send_auth_json(
                    {"ok": True},
                    cookie=self._clear_session_cookie_header(),
                )
                return True
            if path == "/api/v1/auth/password":
                user = service.change_password(
                    principal,
                    str(payload.get("current_password") or ""),
                    str(payload.get("new_password") or ""),
                )
                self._send_json({"ok": True, "user": user})
                return True
            if key_action is not None:
                key_id, action = key_action
                if action == "enable":
                    service.enable_api_key(key_id, principal)
                    self._send_json({"ok": True})
                    return True
            if path == "/api/v1/admin/users":
                user = service.create_user(
                    username=str(payload.get("username") or ""),
                    password=str(payload.get("password") or ""),
                    display_name=str(payload.get("display_name") or ""),
                    role=str(payload.get("role") or "member"),
                    actor_user_id=principal.user_id,
                )
                self._send_json({"ok": True, "user": user}, HTTPStatus.CREATED)
                return True
            if user_action is None:
                return False
            user_id, action = user_action
            if action == "status":
                user = service.set_user_status(
                    user_id,
                    bool(payload.get("active")),
                    principal.user_id,
                    str(payload.get("reason") or ""),
                )
                self._send_json({"ok": True, "user": user})
                return True
            if action == "reset-password":
                user = service.reset_password(
                    user_id, str(payload.get("password") or ""), principal.user_id
                )
                self._send_json({"ok": True, "user": user})
                return True
        except (AuthError, ValueError, json.JSONDecodeError) as exc:
            self._send_auth_exception(exc)
            return True
        return False

    def _personnel_settings(self):
        settings = getattr(self._store(), "_personnel_settings", None)
        if settings is None:
            raise AuthError("人员档案配置尚不可用，请确认服务端已升级", 503, "personnel_settings_unavailable")
        return settings

    def _login_client_ip(self) -> str:
        peer = str(getattr(self, "client_address", ("",))[0]).strip()
        try:
            peer_ip = ip_address(peer)
        except ValueError:
            return peer
        if not peer_ip.is_loopback:
            return peer

        real_ip = str(self.headers.get("X-Real-IP") or "").strip()
        try:
            return str(ip_address(real_ip))
        except ValueError:
            return peer

    def _handle_auth_delete(self, path: str) -> bool:
        service = self._auth_service()
        if service is None:
            return False
        principal = self._require_principal()
        try:
            prefix = "/api/v1/me/keys/"
            if path.startswith(prefix):
                suffix = unquote(path[len(prefix):]).strip("/")
                key_id, separator, action = suffix.partition("/")
                if separator and action == "record":
                    service.delete_api_key(key_id, principal)
                elif not separator:
                    service.revoke_api_key(key_id, principal)
                else:
                    return False
                self._send_json({"ok": True})
                return True
            users_prefix = "/api/v1/admin/users/"
            if path.startswith(users_prefix):
                user_id = unquote(path[len(users_prefix):]).strip("/")
                if user_id and "/" not in user_id:
                    service.delete_user(user_id, principal.user_id)
                    self._send_json({"ok": True})
                    return True
        except (AuthError, ValueError) as exc:
            self._send_auth_exception(exc)
            return True
        return False

    def _admin_user_action(self, path: str) -> tuple[str, str] | None:
        prefix = "/api/v1/admin/users/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):].strip("/")
        user_id, separator, action = suffix.partition("/")
        if not separator or action not in {
            "status", "reset-password", "keys", "service-keys",
        }:
            return None
        return user_id, action

    def _api_key_action(self, path: str) -> tuple[str, str] | None:
        prefix = "/api/v1/me/keys/"
        if not path.startswith(prefix):
            return None
        suffix = unquote(path[len(prefix):]).strip("/")
        key_id, separator, action = suffix.partition("/")
        if not key_id or not separator or action != "enable":
            return None
        return key_id, action

    def _require_principal(self) -> AuthPrincipal:
        principal = self._auth_principal
        if principal is None:
            raise AuthError("authentication is required", 401, "authentication_required")
        return principal

    def _session_cookie(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(str(self.headers.get("Cookie") or ""))
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel is not None else ""

    def _session_cookie_header(self, token: str) -> str:
        header = (
            f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age=43200; "
            "HttpOnly; SameSite=Strict"
        )
        return f"{header}; Secure" if self._request_uses_https() else header

    def _clear_session_cookie_header(self) -> str:
        header = (
            f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; "
            "HttpOnly; SameSite=Strict"
        )
        return f"{header}; Secure" if self._request_uses_https() else header

    def _request_uses_https(self) -> bool:
        forwarded_proto = str(
            self.headers.get("X-Forwarded-Proto") or ""
        ).split(",", 1)[0]
        return forwarded_proto.strip().casefold() == "https"

    def _send_auth_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        cookie: str = "",
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_common_headers("application/json; charset=utf-8", len(body))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_auth_redirect(self, location: str, cookie: str = "") -> None:
        self.send_response(HTTPStatus.FOUND)
        self._send_common_headers("text/plain; charset=utf-8", 0)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _send_auth_error(self, exc: AuthError) -> None:
        self._send_json(
            {"error": str(exc), "code": exc.code},
            HTTPStatus(exc.status),
        )

    def _send_auth_exception(self, exc: Exception) -> None:
        if isinstance(exc, AuthError):
            self._send_auth_error(exc)
            return
        self._send_json(
            {"error": str(exc), "code": "invalid_request"},
            HTTPStatus.BAD_REQUEST,
        )
