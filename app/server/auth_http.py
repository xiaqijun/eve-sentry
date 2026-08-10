"""HTTP authentication middleware and account-management routes."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.cookies import SimpleCookie
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from app.esi.sso import EsiSsoError
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
    "identity_verified",
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


class AuthHttpMixin:
    """Mixin used by the standard-library request handler."""

    _auth_principal: AuthPrincipal | None = None

    def _auth_service(self) -> AuthService | None:
        return getattr(type(self), "auth_service", None)

    def _authorize_request(self, method: str, path: str) -> bool:
        service = self._auth_service()
        self._auth_principal = None
        if service is None:
            return True
        if path in {"/api/health", "/api/livez", "/api/readyz"}:
            return True
        if path == "/api/v1/auth/login" and method == "POST":
            return True
        if path in {
            "/api/v1/auth/esi/start",
            "/api/v1/auth/esi/callback",
        } and method == "GET":
            return True
        if not service.enforce_requests and not self._is_auth_management_path(path):
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

            if principal.is_read_only and method not in {"GET", "HEAD"}:
                raise AuthError("service key is read-only", 403, "read_only_key")
            if principal.is_read_only and path not in {
                "/api/v1/bootstrap",
                "/api/v1/events",
                "/api/v1/alert-history",
                "/api/v1/hostile-waves",
                "/api/v1/integrations/hostile-systems",
            }:
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

    def _is_auth_management_path(self, path: str) -> bool:
        return path.startswith((
            "/api/v1/auth/",
            "/api/v1/me/",
            "/api/v1/admin/",
            "/api/v1/client/identity-check",
        ))

    def _stream_principal_active(self) -> bool:
        service = self._auth_service()
        principal = self._auth_principal
        if service is None or principal is None:
            return True
        return service.is_principal_active(principal)

    def _handle_auth_get(self, path: str) -> bool:
        service = self._auth_service()
        if path == "/api/v1/auth/esi/callback":
            esi_login = self._esi_login()
            owns_callback = getattr(esi_login, "owns_callback", None)
            if callable(owns_callback) and owns_callback(self.path):
                try:
                    esi_login.complete_callback(self.path)
                except EsiSsoError:
                    self._send_auth_redirect("/?esi_login=error")
                    return True
                self._send_auth_redirect("/?esi_login=authenticated")
                return True
            if service is None:
                return False
            try:
                login = service.complete_esi_login(self.path)
            except AuthError as exc:
                self._send_auth_redirect(
                    f"/login?{urlencode({'esi_error': exc.code})}"
                )
                return True
            self._send_auth_redirect(
                str(login["return_to"]),
                cookie=self._session_cookie_header(str(login["session_token"])),
            )
            return True
        if service is None:
            return False
        if path == "/api/v1/auth/esi/start":
            query = parse_qs(urlparse(self.path).query)
            try:
                authorization_url = service.begin_esi_login(
                    str((query.get("return_to") or ["/"])[0])
                )
            except AuthError as exc:
                self._send_auth_redirect(
                    f"/login?{urlencode({'esi_error': exc.code})}"
                )
                return True
            self._send_auth_redirect(authorization_url)
            return True
        auth_paths = {
            "/api/v1/auth/me",
            "/api/v1/me/keys",
            "/api/v1/admin/users",
            "/api/v1/admin/clients",
            "/api/v1/admin/corporations",
            "/api/v1/admin/audit",
            "/api/v1/admin/security-settings",
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
        if path == "/api/v1/admin/security-settings":
            self._send_json({"settings": service.security_settings()})
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
        if path == "/api/v1/admin/corporations":
            self._send_json({"corporations": service.repository.list_allowed_corporations()})
            return True
        if path == "/api/v1/admin/audit":
            self._send_json({"audit": service.repository.list_audit()})
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
            "/api/v1/client/identity-check",
            "/api/v1/client/identity-checks",
            "/api/v1/admin/users",
            "/api/v1/admin/corporations",
            "/api/v1/admin/security-settings",
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
            payload = self._read_optional_json()
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
            if path == "/api/v1/me/keys":
                key = service.create_api_key(
                    principal.user_id,
                    str(payload.get("name") or "Device"),
                    principal.user_id,
                )
                self._send_json({"ok": True, "key": key}, HTTPStatus.CREATED)
                return True
            if key_action is not None:
                key_id, action = key_action
                if action == "enable":
                    service.enable_api_key(key_id, principal)
                    self._send_json({"ok": True})
                    return True
            if path in {
                "/api/v1/client/identity-check",
                "/api/v1/client/identity-checks",
            }:
                names = payload.get("characters", payload.get("names", []))
                if not isinstance(names, list):
                    raise AuthError("characters must be a list", 400, "invalid_characters")
                character_ids = payload.get("character_ids", [])
                if not isinstance(character_ids, list):
                    raise AuthError(
                        "character_ids must be a list",
                        400,
                        "invalid_character_ids",
                    )
                clean_names = [
                    str(item.get("name") if isinstance(item, dict) else item)
                    for item in names
                ]
                character_ids = list(character_ids) + [
                    item.get("character_id")
                    for item in names
                    if isinstance(item, dict)
                    and item.get("character_id") not in {None, ""}
                ]
                if path == "/api/v1/client/identity-checks":
                    result = service.submit_character_report(
                        principal,
                        clean_names,
                        client_id=str(payload.get("client_id") or ""),
                        character_ids=character_ids,
                    )
                    status = (
                        HTTPStatus.ACCEPTED
                        if result.get("pending")
                        else HTTPStatus.OK
                    )
                    self._send_json({"identity": result}, status)
                    return True
                result = service.verify_characters(
                    principal,
                    clean_names,
                    character_ids=character_ids,
                )
                self._send_json({"identity": result})
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
            if path == "/api/v1/admin/corporations":
                corporation = service.add_allowed_corporation(
                    payload.get("corporation_id"), principal.user_id
                )
                self._send_json({"ok": True, "corporation": corporation}, HTTPStatus.CREATED)
                return True
            if path == "/api/v1/admin/security-settings":
                key_risk_control = payload.get("key_risk_control")
                if not isinstance(key_risk_control, bool):
                    raise AuthError(
                        "key_risk_control must be a boolean",
                        400,
                        "invalid_key_risk_control",
                    )
                settings = service.set_key_risk_control(
                    key_risk_control,
                    principal.user_id,
                )
                self._send_json({"ok": True, "settings": settings})
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
            if action == "characters":
                item = service.add_whitelist_character(
                    user_id,
                    payload.get("character_id"),
                    str(payload.get("note") or ""),
                    principal.user_id,
                )
                self._send_json({"ok": True, "character": item}, HTTPStatus.CREATED)
                return True
            if action == "service-keys":
                key = service.create_api_key(
                    user_id,
                    str(payload.get("name") or "Service"),
                    principal.user_id,
                    key_type="service_readonly",
                )
                self._send_json({"ok": True, "key": key}, HTTPStatus.CREATED)
                return True
            if action == "keys":
                key = service.create_api_key(
                    user_id,
                    str(payload.get("name") or "Device"),
                    principal.user_id,
                    key_type=str(payload.get("key_type") or "desktop"),
                )
                self._send_json({"ok": True, "key": key}, HTTPStatus.CREATED)
                return True
        except (AuthError, ValueError, json.JSONDecodeError) as exc:
            self._send_auth_exception(exc)
            return True
        return False

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
            prefix = "/api/v1/admin/corporations/"
            if path.startswith(prefix):
                service.delete_allowed_corporation(
                    int(unquote(path[len(prefix):]).strip()), principal.user_id
                )
                self._send_json({"ok": True})
                return True
            marker = "/characters/"
            users_prefix = "/api/v1/admin/users/"
            if path.startswith(users_prefix) and marker in path:
                suffix = path[len(users_prefix):]
                user_id, character_id = suffix.split(marker, 1)
                service.delete_whitelist_character(
                    user_id.strip(), int(unquote(character_id).strip()), principal.user_id
                )
                self._send_json({"ok": True})
                return True
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
            "status", "reset-password", "characters", "keys", "service-keys",
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
