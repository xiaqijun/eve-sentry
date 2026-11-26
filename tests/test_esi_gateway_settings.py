"""ESI gateway settings use authenticated, tested changes to approved targets."""

import os
from types import SimpleNamespace

import pytest

from app.esi.transport import EsiConnections, TransportEsiClient
from app.server.auth import AuthError, AuthService
from app.server.auth_store import AuthRepository
from app.server.esi_gateway_settings import EsiGatewaySettings
from app.server.http_server import IntelHTTPServer
from tests.auth_test_store import AuthTestStore
from tests.test_http_server import authenticated_request


STARTUP_URL = "http://10.1.2.3:8787"
NEXT_URL = "http://10.1.2.4:8787"
STARTUP_TOKEN = "startup-gateway-token-" + "1" * 20
NEXT_TOKEN = "replacement-gateway-token-" + "2" * 20
ENVIRONMENT = {
    "EVE_SENTRY_ESI_TRANSPORT": "relay",
    "EVE_SENTRY_SERVER_ESI_GATEWAY_ALLOWED_URLS": NEXT_URL,
}


@pytest.fixture
def manager(tmp_path):
    args = SimpleNamespace(config=str(tmp_path / "intel.json"), esi_backend="local",
                           esi_gateway_url=STARTUP_URL, esi_gateway_token=STARTUP_TOKEN)
    settings = EsiGatewaySettings(args, environment=ENVIRONMENT)
    pool = EsiConnections(relay_host="10.1.2.3", relay_token=STARTUP_TOKEN)
    settings.bind(TransportEsiClient(pool))
    return settings, pool, args


def test_approved_change_preserves_runtime_budgets_and_reloads(manager, monkeypatch):
    settings, pool, args = manager
    checked = []
    monkeypatch.setattr(EsiConnections, "test_relay", lambda candidate: checked.append(candidate.relay_target))
    pool.blocked_until = 42

    result = settings.update({"url": NEXT_URL, "revision": "environment", "token": NEXT_TOKEN})

    assert checked == [("10.1.2.4", 8787, NEXT_TOKEN)]
    assert pool.relay_target == checked[0] and pool.blocked_until == 42
    assert result["url"] == NEXT_URL and result["token_configured"] is True
    assert "token" not in result and NEXT_TOKEN not in str(result)
    if os.name != "nt":
        assert settings.path.stat().st_mode & 0o077 == 0
    restored = EsiGatewaySettings(args, environment=ENVIRONMENT)
    restored.load(args)
    assert (args.esi_gateway_url, args.esi_gateway_token) == (NEXT_URL, NEXT_TOKEN)
    assert restored.snapshot()["revision"] == result["revision"]
    with pytest.raises(AuthError) as conflict:
        settings.update({"url": STARTUP_URL, "revision": "environment", "token": STARTUP_TOKEN})
    assert conflict.value.status == 409


def test_invalid_or_unverified_changes_keep_startup_route(manager, monkeypatch):
    settings, pool, _ = manager
    attempts = []

    def reject(candidate):
        attempts.append(candidate.relay_target)
        raise OSError("test connection failed")

    monkeypatch.setattr(EsiConnections, "test_relay", reject)
    with pytest.raises(ValueError, match="未列入"):
        settings.update({"url": "http://10.1.2.5:8787", "revision": "environment", "token": NEXT_TOKEN})
    with pytest.raises(ValueError, match="令牌"):
        settings.update({"url": NEXT_URL, "revision": "environment"})
    assert attempts == []
    with pytest.raises(AuthError) as denied:
        settings.update({"url": NEXT_URL, "revision": "environment", "token": NEXT_TOKEN})
    assert denied.value.status == 502
    assert len(attempts) == 1
    assert settings.snapshot()["url"] == STARTUP_URL and pool.relay_host == "10.1.2.3"
    assert not settings.path.exists()


def test_routes_require_admin_session_and_csrf(manager, tmp_path, monkeypatch):
    settings, _, _ = manager
    monkeypatch.setattr(EsiConnections, "test_relay", lambda candidate: None)
    store = AuthTestStore(tmp_path / "reports.json")
    store._esi_gateway_settings = settings
    auth = AuthService(AuthRepository(store._connect), None)
    admin = auth.create_user("operator", "operator-password-123", role="admin")
    key = auth.create_api_key(admin["user_id"], "test", admin["user_id"])
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    try:
        base = f"{server.url}/api/v1"
        route = f"{base}/admin/esi-gateway/settings"
        test_route = f"{base}/admin/esi-gateway/test"
        assert authenticated_request(route)[0] == 401
        assert authenticated_request(route, headers={"Authorization": "Bearer " + key["secret"]})[0] == 403
        status, headers, body = authenticated_request(f"{base}/auth/login", "POST", {
            "username": "operator", "password": "operator-password-123",
        })
        assert status == 200
        cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        session = {**cookie, "X-CSRF-Token": body["csrf_token"]}
        change = {"url": NEXT_URL, "revision": "environment", "token": NEXT_TOKEN}
        assert authenticated_request(route, "POST", change, cookie)[0] == 403
        status, headers, body = authenticated_request(route, "POST", change, session)
        assert status == 200 and body["settings"]["url"] == NEXT_URL
        assert headers["Cache-Control"] == "no-store" and NEXT_TOKEN not in str(body)
        monkeypatch.setattr(settings, "next_test", 0)
        assert authenticated_request(test_route, "POST", {}, session)[2] == {"ok": True}
        assert authenticated_request(route, headers=session)[2]["settings"]["url"] == NEXT_URL
    finally:
        server.stop()
        auth.close()
        store.close()
