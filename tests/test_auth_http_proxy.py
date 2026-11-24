"""Tests for trusted reverse-proxy client addresses during login."""

from app.server.auth_http import AuthHttpMixin


class RecordingAuthService:
    def __init__(self) -> None:
        self.client_ips: list[str] = []

    def login(self, username: str, password: str, client_ip: str) -> dict:
        self.client_ips.append(client_ip)
        return {
            "user": {"username": username},
            "csrf_token": "csrf-token",
            "session_token": "session-token",
        }


class AuthHandlerStub(AuthHttpMixin):
    auth_service: RecordingAuthService

    def __init__(
        self,
        service: RecordingAuthService,
        peer_ip: str,
        headers: dict[str, str],
    ) -> None:
        type(self).auth_service = service
        self.client_address = (peer_ip, 12345)
        self.headers = headers

    def _read_json(self) -> dict[str, str]:
        return {"username": "admin", "password": "secret"}

    def _send_auth_json(self, payload: dict, status=200, cookie: str = "") -> None:
        return None


def _login_client_ip(peer_ip: str, headers: dict[str, str]) -> str:
    service = RecordingAuthService()
    handler = AuthHandlerStub(service, peer_ip, headers)

    assert handler._handle_auth_post("/api/v1/auth/login") is True
    return service.client_ips[0]


def test_login_trusts_valid_x_real_ip_from_loopback_proxy() -> None:
    client_ip = _login_client_ip(
        "127.0.0.1",
        {
            "X-Real-IP": "198.51.100.23",
            "X-Forwarded-For": "203.0.113.99",
        },
    )

    assert client_ip == "198.51.100.23"


def test_login_ignores_spoofed_x_real_ip_from_non_loopback_peer() -> None:
    client_ip = _login_client_ip(
        "203.0.113.8",
        {"X-Real-IP": "198.51.100.23"},
    )

    assert client_ip == "203.0.113.8"


def test_login_falls_back_to_loopback_peer_for_invalid_x_real_ip() -> None:
    client_ip = _login_client_ip(
        "::1",
        {
            "X-Real-IP": "not-an-ip",
            "X-Forwarded-For": "198.51.100.23",
        },
    )

    assert client_ip == "::1"
