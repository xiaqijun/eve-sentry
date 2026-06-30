"""EVE SSO OAuth2 helpers with PKCE support."""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import hashlib
import json
import secrets
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_AUTHORIZATION_ENDPOINT = "https://login.eveonline.com/v2/oauth/authorize"
DEFAULT_TOKEN_ENDPOINT = "https://login.eveonline.com/v2/oauth/token"
DEFAULT_METADATA_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
DEFAULT_SCOPES = (
    "esi-location.read_location.v1",
    "esi-characters.read_contacts.v1",
)


class EsiSsoError(RuntimeError):
    """Raised when SSO authorization or token handling fails."""


class TokenProtector:
    """Encrypt and decrypt token payload bytes for local storage."""

    name = "plain"

    def protect(self, data: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, data: bytes) -> bytes:
        raise NotImplementedError


class WindowsDpapiTokenProtector(TokenProtector):
    """Protect local token files with the current Windows user profile."""

    name = "windows-dpapi"

    @classmethod
    def is_available(cls) -> bool:
        return sys.platform == "win32"

    def protect(self, data: bytes) -> bytes:
        return _crypt_protect_data(data)

    def unprotect(self, data: bytes) -> bytes:
        return _crypt_unprotect_data(data)


@dataclass(frozen=True)
class SsoMetadata:
    """OAuth endpoint metadata needed by the local SSO client."""

    authorization_endpoint: str = DEFAULT_AUTHORIZATION_ENDPOINT
    token_endpoint: str = DEFAULT_TOKEN_ENDPOINT
    revocation_endpoint: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SsoMetadata":
        return cls(
            authorization_endpoint=str(
                payload.get("authorization_endpoint")
                or DEFAULT_AUTHORIZATION_ENDPOINT
            ),
            token_endpoint=str(payload.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT),
            revocation_endpoint=str(payload.get("revocation_endpoint") or ""),
        )


@dataclass(frozen=True)
class PkceChallenge:
    """PKCE verifier/challenge pair."""

    verifier: str
    challenge: str
    method: str = "S256"


@dataclass(frozen=True)
class AuthorizationSession:
    """State needed to finish one browser authorization attempt."""

    authorization_url: str
    state: str
    redirect_uri: str
    code_verifier: str
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TokenSet:
    """OAuth tokens and decoded EVE character ownership metadata."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 0
    refresh_token: str = ""
    scopes: list[str] = field(default_factory=list)
    character_id: int | None = None
    character_owner_hash: str = ""
    expires_at: float = 0.0

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        now: Callable[[], float] | None = None,
    ) -> "TokenSet":
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise EsiSsoError("token response did not include access_token")

        clock = now or time
        claims = decode_jwt_claims(access_token)
        expires_in = _optional_int(payload.get("expires_in")) or 0
        expires_at = float(payload.get("expires_at") or 0)
        if expires_at <= 0:
            expires_at = clock() + max(0, expires_in)

        return cls(
            access_token=access_token,
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_in=expires_in,
            refresh_token=str(payload.get("refresh_token") or ""),
            scopes=_token_scopes(payload, claims),
            character_id=_character_id_from_payload(payload, claims),
            character_owner_hash=str(
                payload.get("character_owner_hash")
                or payload.get("CharacterOwnerHash")
                or claims.get("owner")
                or claims.get("CharacterOwnerHash")
                or ""
            ),
            expires_at=expires_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scopes": list(self.scopes),
            "character_id": self.character_id,
            "character_owner_hash": self.character_owner_hash,
            "expires_at": self.expires_at,
        }

    def is_expired(self, now: Callable[[], float] | None = None, skew: int = 60) -> bool:
        clock = now or time
        return self.expires_at <= clock() + max(0, int(skew))


class EsiTokenStore:
    """JSON-backed token storage for local desktop SSO sessions."""

    def __init__(
        self,
        path: str | Path = "esi_tokens.json",
        protector: TokenProtector | None = None,
    ) -> None:
        self.path = Path(path)
        self.protector = protector

    @property
    def is_secure(self) -> bool:
        return self.protector is not None

    def load(self) -> TokenSet | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("protected") is True:
            payload = self._unprotect_payload(payload)
            if payload is None:
                return None
        try:
            return TokenSet.from_payload(payload)
        except EsiSsoError:
            return None

    def save(self, tokens: TokenSet) -> None:
        payload = tokens.to_dict()
        if self.protector is not None:
            payload = self._protect_payload(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return

    def _protect_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.protector is None:
            return payload
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        protected = self.protector.protect(raw)
        return {
            "version": 1,
            "protected": True,
            "provider": self.protector.name,
            "payload": base64.b64encode(protected).decode("ascii"),
        }

    def _unprotect_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        protected = str(payload.get("payload") or "")
        if not protected:
            return None
        protector = self.protector or token_protector_from_name(
            str(payload.get("provider") or "")
        )
        if protector is None:
            return None
        try:
            encrypted = base64.b64decode(protected.encode("ascii"), validate=True)
            raw = protector.unprotect(encrypted)
            data = json.loads(raw.decode("utf-8"))
        except (
            ValueError,
            OSError,
            binascii.Error,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ):
            return None
        return data if isinstance(data, dict) else None


class LocalCallbackServer:
    """Temporary localhost callback receiver for interactive SSO login."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8766,
        path: str = "/callback",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else f"/{path}"
        self._event = threading.Event()
        self._callback_url = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_redirect_uri(cls, redirect_uri: str) -> "LocalCallbackServer":
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http":
            raise ValueError("redirect_uri must use http for local callback")
        return cls(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 80,
            path=parsed.path or "/callback",
        )

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._handler())
        self.host, self.port = self._httpd.server_address[:2]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="eve-sentry-sso-callback",
            daemon=True,
        )
        self._thread.start()

    def wait_for_callback(self, timeout_seconds: float = 300.0) -> str:
        if not self._event.wait(timeout_seconds):
            raise EsiSsoError("timed out waiting for SSO callback")
        return self._callback_url

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "EveSentrySSO/1.0"

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != outer.path:
                    self._send_text("Not found", HTTPStatus.NOT_FOUND)
                    return
                host = self.headers.get("Host") or f"{outer.host}:{outer.port}"
                outer._callback_url = f"http://{host}{self.path}"
                outer._event.set()
                self._send_text("EVE Sentry SSO complete. You can close this tab.")

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_text(
                self,
                text: str,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def build_token_store(
    path: str | Path = "esi_tokens.json",
    storage: str = "auto",
) -> EsiTokenStore:
    """Create a token store using plain or platform-protected storage."""
    mode = str(storage or "auto").strip().casefold()
    if mode == "plain":
        return EsiTokenStore(path)
    if mode not in {"auto", "secure"}:
        raise ValueError("storage must be one of auto, secure, or plain")

    protector = default_token_protector()
    if protector is not None:
        return EsiTokenStore(path, protector=protector)
    if mode == "secure":
        raise EsiSsoError("secure ESI token storage is not available")
    return EsiTokenStore(path)


def default_token_protector() -> TokenProtector | None:
    """Return the best local token protector available on this platform."""
    if WindowsDpapiTokenProtector.is_available():
        return WindowsDpapiTokenProtector()
    return None


def token_protector_from_name(name: str) -> TokenProtector | None:
    """Return a protector able to read a saved protected token payload."""
    provider = name.strip().casefold()
    if (
        provider == WindowsDpapiTokenProtector.name
        and WindowsDpapiTokenProtector.is_available()
    ):
        return WindowsDpapiTokenProtector()
    return None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _crypt_protect_data(data: bytes) -> bytes:
    if not WindowsDpapiTokenProtector.is_available():
        raise EsiSsoError("Windows DPAPI is not available")
    return _crypt_data(data, protect=True)


def _crypt_unprotect_data(data: bytes) -> bytes:
    if not WindowsDpapiTokenProtector.is_available():
        raise EsiSsoError("Windows DPAPI is not available")
    return _crypt_data(data, protect=False)


def _crypt_data(data: bytes, protect: bool) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    data_in = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    data_out = _DataBlob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if protect:
        success = crypt32.CryptProtectData(
            ctypes.byref(data_in),
            "EVE Sentry ESI token",
            None,
            None,
            None,
            0,
            ctypes.byref(data_out),
        )
    else:
        success = crypt32.CryptUnprotectData(
            ctypes.byref(data_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(data_out),
        )
    if not success:
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        kernel32.LocalFree(data_out.pbData)


class EveSsoClient:
    """Small OAuth2 client for EVE SSO public-client PKCE flows."""

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: list[str] | tuple[str, ...] | None = None,
        metadata: SsoMetadata | None = None,
        metadata_url: str = DEFAULT_METADATA_URL,
        timeout: float = 10.0,
        user_agent: str = "eve-sentry/0.1",
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        if not self.client_id:
            raise ValueError("client_id must be non-empty")
        self.redirect_uri = redirect_uri.strip()
        if not self.redirect_uri:
            raise ValueError("redirect_uri must be non-empty")
        self.scopes = _normalize_scopes(scopes or DEFAULT_SCOPES)
        self.metadata = metadata or SsoMetadata()
        self.metadata_url = metadata_url
        self.timeout = timeout
        self.user_agent = user_agent
        self._opener = opener or urlopen

    def fetch_metadata(self) -> SsoMetadata:
        request = Request(
            self.metadata_url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise EsiSsoError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise EsiSsoError("SSO metadata response was not a JSON object")
        self.metadata = SsoMetadata.from_payload(payload)
        return self.metadata

    def create_authorization_session(
        self,
        scopes: list[str] | tuple[str, ...] | None = None,
        state: str | None = None,
    ) -> AuthorizationSession:
        scope_values = _normalize_scopes(scopes or self.scopes)
        state_value = state or secrets.token_urlsafe(32)
        pkce = create_pkce_challenge()
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scope_values),
            "state": state_value,
            "code_challenge": pkce.challenge,
            "code_challenge_method": pkce.method,
        }
        return AuthorizationSession(
            authorization_url=(
                f"{self.metadata.authorization_endpoint}?{urlencode(query)}"
            ),
            state=state_value,
            redirect_uri=self.redirect_uri,
            code_verifier=pkce.verifier,
            scopes=scope_values,
        )

    def parse_callback_url(
        self,
        session: AuthorizationSession,
        callback_url: str,
    ) -> str:
        query = parse_qs(urlparse(callback_url).query)
        returned_state = (query.get("state") or [""])[0]
        if returned_state != session.state:
            raise EsiSsoError("SSO callback state did not match")
        error = (query.get("error") or [""])[0]
        if error:
            description = (query.get("error_description") or [""])[0]
            raise EsiSsoError(description or error)
        code = (query.get("code") or [""])[0]
        if not code:
            raise EsiSsoError("SSO callback did not include code")
        return code

    def exchange_code(
        self,
        code: str,
        session: AuthorizationSession,
    ) -> TokenSet:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": self.client_id,
                "redirect_uri": session.redirect_uri,
                "code_verifier": session.code_verifier,
            }
        )

    def refresh(self, refresh_token: str, scopes: list[str] | None = None) -> TokenSet:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token.strip(),
            "client_id": self.client_id,
        }
        if scopes is not None:
            payload["scope"] = " ".join(_normalize_scopes(scopes))
        return self._token_request(payload)

    def _token_request(self, payload: dict[str, str]) -> TokenSet:
        data = urlencode(payload).encode("utf-8")
        request = Request(
            self.metadata.token_endpoint,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise EsiSsoError(_read_http_error(exc)) from exc
        except (URLError, OSError) as exc:
            raise EsiSsoError(str(exc)) from exc

        try:
            token_payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EsiSsoError("SSO token response was not valid JSON") from exc
        if not isinstance(token_payload, dict):
            raise EsiSsoError("SSO token response was not a JSON object")
        return TokenSet.from_payload(token_payload)


def run_local_sso_login(
    client: EveSsoClient,
    token_store: EsiTokenStore,
    timeout_seconds: float = 300.0,
    open_browser: bool = True,
    browser_open: Callable[[str], Any] | None = None,
    announce_url: Callable[[str], Any] | None = None,
) -> TokenSet:
    """Complete an interactive localhost PKCE login and persist tokens."""
    server = LocalCallbackServer.from_redirect_uri(client.redirect_uri)
    server.start()
    try:
        session = client.create_authorization_session()
        if announce_url is not None:
            announce_url(session.authorization_url)
        if open_browser:
            (browser_open or webbrowser.open)(session.authorization_url)
        callback_url = server.wait_for_callback(timeout_seconds)
        code = client.parse_callback_url(session, callback_url)
        tokens = client.exchange_code(code, session)
        token_store.save(tokens)
        return tokens
    finally:
        server.stop()


def create_pkce_challenge(verifier: str | None = None) -> PkceChallenge:
    verifier_value = verifier or secrets.token_urlsafe(64)
    return PkceChallenge(
        verifier=verifier_value,
        challenge=build_pkce_challenge(verifier_value),
    )


def build_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = _base64url_decode(parts[1]).decode("utf-8")
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _token_scopes(payload: dict[str, Any], claims: dict[str, Any]) -> list[str]:
    for key in ("scopes", "scope"):
        scopes = _normalize_scope_value(payload.get(key))
        if scopes:
            return scopes
    scopes = _normalize_scope_value(claims.get("scp"))
    if scopes:
        return scopes
    return _normalize_scope_value(claims.get("scope"))


def _normalize_scope_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split()
    elif isinstance(value, list):
        values = [str(item) for item in value]
    else:
        return []
    return _normalize_scopes(values)


def _normalize_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for scope in scopes:
        text = str(scope).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _character_id_from_payload(
    payload: dict[str, Any],
    claims: dict[str, Any],
) -> int | None:
    direct = _optional_int(payload.get("character_id") or payload.get("CharacterID"))
    if direct is not None:
        return direct
    claim_id = _optional_int(claims.get("character_id") or claims.get("CharacterID"))
    if claim_id is not None:
        return claim_id
    subject = str(claims.get("sub") or "")
    return _character_id_from_subject(subject)


def _character_id_from_subject(subject: str) -> int | None:
    parts = subject.split(":")
    if len(parts) >= 3 and parts[-2].casefold() == "eve":
        return _optional_int(parts[-1])
    return _optional_int(subject)


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _read_http_error(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        if isinstance(payload, dict):
            return str(payload.get("error_description") or payload.get("error") or exc)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return f"SSO HTTP {exc.code}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run EVE SSO login for EVE Sentry")
    parser.add_argument("--client-id", required=True)
    parser.add_argument(
        "--redirect-uri",
        default="http://127.0.0.1:8766/callback",
    )
    parser.add_argument("--token-file", default="esi_tokens.json")
    parser.add_argument(
        "--token-storage",
        choices=["auto", "secure", "plain"],
        default="auto",
        help="token storage protection mode",
    )
    parser.add_argument("--scope", action="append", dest="scopes")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = EveSsoClient(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scopes=args.scopes or DEFAULT_SCOPES,
    )
    token_store = build_token_store(args.token_file, storage=args.token_storage)
    tokens = run_local_sso_login(
        client,
        token_store,
        timeout_seconds=args.timeout,
        open_browser=not args.no_browser,
        announce_url=lambda url: print(f"Open this URL to authorize:\n{url}"),
    )
    character = tokens.character_id or "unknown"
    storage = "secure" if token_store.is_secure else "plain"
    print(
        f"Saved ESI token for character {character} to {token_store.path} "
        f"({storage} storage)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
