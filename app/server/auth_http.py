"""HTTP authentication middleware and account-management routes."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from app.esi.sso import EsiSsoError
from app.server.auth import AuthError, AuthPrincipal, AuthService, SESSION_COOKIE_NAME


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
        if path == "/api/health":
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
                principal = service.authenticate_api_key(
                    secret,
                    allow_unverified=path == "/api/v1/client/identity-check",
                )
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
            }:
                raise AuthError(
                    "service key can only read Bootstrap and SSE",
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
            "/api/v1/admin/corporations",
            "/api/v1/admin/audit",
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
            "/api/v1/admin/users",
            "/api/v1/admin/corporations",
        }
        user_action = self._admin_user_action(path)
        if path not in auth_paths and user_action is None:
            return False
        try:
            if path == "/api/v1/auth/login":
                payload = self._read_json()
                login = service.login(
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                    str(getattr(self, "client_address", ("",))[0]),
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
            if path == "/api/v1/client/identity-check":
                names = payload.get("characters", payload.get("names", []))
                if not isinstance(names, list):
                    raise AuthError("characters must be a list", 400, "invalid_characters")
                clean_names = [
                    str(item.get("name") if isinstance(item, dict) else item)
                    for item in names
                ]
                result = service.verify_characters(principal, clean_names)
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
        except (AuthError, ValueError, json.JSONDecodeError) as exc:
            self._send_auth_exception(exc)
            return True
        return False

    def _handle_auth_delete(self, path: str) -> bool:
        service = self._auth_service()
        if service is None:
            return False
        principal = self._require_principal()
        try:
            prefix = "/api/v1/me/keys/"
            if path.startswith(prefix):
                service.revoke_api_key(unquote(path[len(prefix):]).strip(), principal)
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
            "status", "reset-password", "characters", "service-keys",
        }:
            return None
        return user_id, action

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
