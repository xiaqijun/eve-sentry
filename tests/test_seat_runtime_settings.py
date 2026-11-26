import hashlib
import json
from urllib.request import Request, urlopen

from app.server.auth import AuthService, SESSION_COOKIE_NAME
from app.server.auth_store import AuthRepository
from app.server.http_server import IntelHTTPServer
from app.server.seat_token_vault import SEAT_TOKEN_COPY_SETTING, SeatTokenVault
from tests.auth_test_store import AuthTestStore
from tests.test_http_server import _create_seat_http_key, authenticated_request


ADMIN_PATH = "/api/v1/admin/seat-integration"
COPY_PATH = ADMIN_PATH + "/copy-token"
SEAT_PATH = "/api/v1/integrations/seat/alert-consumption"


def browser_session(auth, username):
    login = auth.login(username, "test-password", "127.0.0.1")
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={login['session_token']}",
        "X-CSRF-Token": login["csrf_token"],
    }


def test_admin_switches_editions_and_restarts_with_saved_mode(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    repository = AuthRepository(store._connect)
    auth = AuthService(repository, None, seat_auth_mode="enforce")
    admin = auth.create_user("admin", "test-password", role="admin")
    auth.create_user("member", "test-password")
    legacy = auth.create_api_key(admin["user_id"], "legacy", admin["user_id"])
    seat = _create_seat_http_key(auth, admin["user_id"])
    seat_headers = {"Authorization": "Bearer " + seat["secret"]}
    token = "s" * 32
    service_headers = {"Authorization": "Bearer " + token}
    server = IntelHTTPServer(store, port=0, auth_service=auth, seat_integration_token=token)
    server.start()
    try:
        admin_headers = browser_session(auth, "admin")
        member_headers = browser_session(auth, "member")
        client_probe = server.url + "/api/v1/auth/me"
        settings_url = server.url + ADMIN_PATH
        integration_url = server.url + SEAT_PATH
        assert authenticated_request(settings_url)[0] == 401
        assert authenticated_request(settings_url, headers=member_headers)[0] == 403
        assert authenticated_request(settings_url, headers={
            "Authorization": "Bearer " + legacy["secret"],
        })[0] == 403
        status, headers, body = authenticated_request(settings_url, headers=admin_headers)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert body["settings"]["enabled"] and body["settings"]["revision"] == 0
        assert body["settings"]["token_configured"] and token not in str(body)
        assert authenticated_request(integration_url, headers=service_headers)[0] == 200
        assert authenticated_request(settings_url, headers=admin_headers)[2]["settings"]["last_authenticated_at"]
        disable = {"enabled": False, "revision": 0}
        assert authenticated_request(settings_url, "POST", disable, {
            "Cookie": admin_headers["Cookie"],
        })[0] == 403

        stream_request = Request(
            server.url + "/api/v1/events?timeout=30&heartbeat=15&bootstrap=0",
            headers=seat_headers,
        )
        with urlopen(stream_request, timeout=3) as response:
            assert response.readline().startswith(b": connected")
            status, _, body = authenticated_request(settings_url, "POST", disable, admin_headers)
            assert status == 200 and body["settings"]["local_issuance"]
            assert body["settings"]["last_authenticated_at"] is None
            response.read()
        assert authenticated_request(client_probe, headers=seat_headers)[0] == 401
        assert authenticated_request(integration_url, headers=service_headers)[0] == 503
        restarted = AuthService(repository, None, seat_auth_mode="enforce")
        assert restarted.local_client_keys_enabled

        status, _, body = authenticated_request(
            server.url + "/api/v1/me/keys", "POST", {"name": "monitor"}, member_headers,
        )
        assert status == 201
        local = body["key"]["secret"]
        local_headers = {"Authorization": "Bearer " + local}
        assert authenticated_request(client_probe, headers=local_headers)[0] == 200
        assert authenticated_request(settings_url, "POST", {"enabled": True, "revision": 0}, admin_headers)[0] == 409
        assert authenticated_request(settings_url, "POST", {"enabled": "true", "revision": 1}, admin_headers)[0] == 400
        assert authenticated_request(settings_url, "POST", {"enabled": True, "revision": 1}, member_headers)[0] == 403
        status, _, body = authenticated_request(settings_url, "POST", {"enabled": True, "revision": 1}, admin_headers)
        assert status == 200 and body["settings"]["enabled"] and body["settings"]["revision"] == 2
        assert authenticated_request(client_probe, headers=local_headers)[0] == 401
        assert authenticated_request(client_probe, headers=seat_headers)[0] == 200
        restarted = AuthService(repository, None, local_client_keys_enabled=True)
        assert restarted.authenticate_api_key(seat["secret"]).seat_enabled_at
    finally:
        server.stop()
        store.close()


def test_unconfigured_server_refuses_to_enable_seat(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    auth = AuthService(AuthRepository(store._connect), None, local_client_keys_enabled=True)
    auth.create_user("admin", "test-password", role="admin")
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    try:
        headers = browser_session(auth, "admin")
        status, _, body = authenticated_request(
            server.url + ADMIN_PATH, "POST", {"enabled": True, "revision": 0}, headers,
        )
        assert status == 409 and body["code"] == "seat_token_required"
        assert auth.local_client_keys_enabled
    finally:
        server.stop()
        store.close()


def test_web_token_is_returned_on_generation_but_not_in_settings(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    repository = AuthRepository(store._connect)
    auth = AuthService(repository, None, seat_auth_mode="enforce")
    auth.create_user("admin", "test-password", role="admin")
    startup = "s" * 32
    server = IntelHTTPServer(store, port=0, auth_service=auth, seat_integration_token=startup)
    server.start()
    try:
        admin = browser_session(auth, "admin")
        settings = server.url + ADMIN_PATH
        integration = server.url + SEAT_PATH
        status, headers, body = authenticated_request(
            settings, "POST", {"revision": 0, "generate_service_token": True}, admin,
        )
        assert status == 200 and headers["Cache-Control"] == "no-store"
        token = body["service_token"]
        assert len(token) == 64
        assert body["settings"]["enabled"] and body["settings"]["token_configured"]
        assert token not in repository.setting("seat_runtime")
        assert token not in str(authenticated_request(settings, headers=admin)[2])
        assert authenticated_request(integration, headers={"Authorization": "Bearer " + startup})[0] == 401
        assert authenticated_request(integration, headers={"Authorization": "Bearer " + token})[0] == 200
        restarted = AuthService(repository, None, seat_auth_mode="enforce")
        restarted.authenticate_seat_service_token(token, "")

        supplied = "manual-token-" * 3
        status, _, body = authenticated_request(
            settings, "POST", {"revision": 1, "service_token": supplied}, admin,
        )
        assert status == 200 and "service_token" not in body
        assert supplied not in repository.setting("seat_runtime")
        assert authenticated_request(integration, headers={"Authorization": "Bearer " + token})[0] == 401
        assert authenticated_request(integration, headers={"Authorization": "Bearer " + supplied})[0] == 200
    finally:
        server.stop()
        store.close()


def test_admin_copies_existing_token_after_seat_authentication(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    repository = AuthRepository(store._connect)
    token = "current-seat-token-" * 3
    startup = "previous-startup-token-" * 3
    service_headers = {"Authorization": "Bearer " + token}
    repository.set_setting("seat_runtime", json.dumps({
        "enabled": True,
        "revision": 4,
        "enabled_at": "",
        "service_token_hash": hashlib.sha256(token.encode()).hexdigest(),
    }), "2026-11-26T00:00:00+00:00")
    key_path = tmp_path / "seat-token.key"
    vault = SeatTokenVault(repository, key_path)
    auth = AuthService(repository, None, seat_auth_mode="enforce", seat_token_vault=vault)
    administrator = auth.create_user("admin", "test-password", role="admin")
    auth.create_user("member", "test-password")
    server = IntelHTTPServer(store, port=0, auth_service=auth, seat_integration_token=startup)
    server.start()
    try:
        admin = browser_session(auth, "admin")
        member = browser_session(auth, "member")
        copy_url = server.url + COPY_PATH
        assert authenticated_request(copy_url, "POST", {})[0] == 401
        assert authenticated_request(copy_url, "POST", {}, member)[0] == 403
        assert authenticated_request(copy_url, "POST", {}, {"Cookie": admin["Cookie"]})[0] == 403
        assert authenticated_request(copy_url, headers=admin)[0] == 404
        status, _, payload = authenticated_request(copy_url, "POST", {}, admin)
        assert status == 409 and payload["code"] == "seat_token_copy_pending"

        integration_url = server.url + SEAT_PATH
        assert authenticated_request(integration_url, headers={"Authorization": "Bearer " + startup})[0] == 401
        assert repository.setting(SEAT_TOKEN_COPY_SETTING) is None
        assert authenticated_request(integration_url, headers=service_headers)[0] == 200

        status, headers, payload = authenticated_request(copy_url, "POST", {}, admin)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert payload == {"service_token": token}
        settings = authenticated_request(server.url + ADMIN_PATH, headers=admin)[2]
        assert settings["settings"]["revision"] == 4 and token not in str(settings)
        ciphertext = repository.setting(SEAT_TOKEN_COPY_SETTING)
        assert ciphertext and token not in ciphertext
        audit = next(row for row in repository.list_audit() if row["action"] == "seat.integration.token_copy")
        assert audit["actor_user_id"] == administrator["user_id"] and token not in str(audit)

        restarted = AuthService(
            repository, None, seat_auth_mode="enforce", seat_token_vault=SeatTokenVault(repository, key_path),
        )
        assert restarted.copy_seat_service_token(startup, administrator["user_id"]) == token
        assert restarted.seat_integration_status(startup)["revision"] == 4

        status, _, payload = authenticated_request(
            server.url + ADMIN_PATH, "POST", {"revision": 4, "generate_service_token": True}, admin,
        )
        assert status == 200
        replacement = payload["service_token"]
        assert replacement != token
        assert authenticated_request(copy_url, "POST", {}, admin)[2]["service_token"] == replacement
        assert authenticated_request(integration_url, headers=service_headers)[0] == 401
    finally:
        server.stop()
        store.close()


def test_startup_token_is_copyable_without_a_seat_request(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    auth = AuthService(AuthRepository(store._connect), None, seat_auth_mode="enforce")
    token = "startup-token-" * 3
    try:
        assert auth.copy_seat_service_token(token, "admin") == token
        assert auth.seat_integration_status(token)["revision"] == 0
    finally:
        store.close()


def test_existing_token_authenticates_when_copy_cannot_be_persisted(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    repository = AuthRepository(store._connect)
    token = "legacy-service-token-" * 3
    key_path = tmp_path / "seat-token.key"
    key_path.mkdir()
    old_service = AuthService(repository, None, seat_auth_mode="enforce")
    old_service.apply_seat_integration_settings({"revision": 0, "service_token": token}, "")
    auth = AuthService(
        repository, None, seat_auth_mode="enforce", seat_token_vault=SeatTokenVault(repository, key_path),
    )
    try:
        auth.authenticate_seat_service_token(token, "")
        assert auth.copy_seat_service_token("", "admin") == token
        assert repository.setting(SEAT_TOKEN_COPY_SETTING) is None
    finally:
        store.close()


def test_provisioning_token_does_not_enable_legacy_seat_integration(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    auth = AuthService(AuthRepository(store._connect), None)
    auth.create_user("admin", "test-password", role="admin")
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    try:
        admin = browser_session(auth, "admin")
        endpoint = server.url + ADMIN_PATH
        status, _, body = authenticated_request(
            endpoint, "POST", {"revision": 0, "generate_service_token": True}, admin,
        )
        assert status == 200 and body["settings"]["enabled"] is False
        service = {"Authorization": "Bearer " + body["service_token"]}
        assert authenticated_request(server.url + SEAT_PATH, headers=service)[0] == 503
        assert authenticated_request(endpoint, "POST", {"revision": 1, "enabled": True}, admin)[0] == 200
        assert authenticated_request(server.url + SEAT_PATH, headers=service)[0] == 200
    finally:
        server.stop()
        store.close()
