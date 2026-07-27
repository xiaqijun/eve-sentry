from app.server.auth import AuthService
from app.server.auth_store import AuthRepository
from app.server.http_server import IntelHTTPServer
from app.server.sqlite_store import SQLiteIntelStore
from tests.test_http_server import AuthTestResolver, authenticated_request


def test_setup_mode_keeps_data_open_but_protects_auth_management(tmp_path):
    store = SQLiteIntelStore(tmp_path / "intel.sqlite3")
    auth = AuthService(
        AuthRepository(store._connect),
        AuthTestResolver(),
        enforce_requests=False,
    )
    auth.create_user("admin", "admin-password-123", role="admin")
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    try:
        status, _, payload = authenticated_request(f"{server.url}/api/v1/bootstrap")
        assert status == 200
        assert "bootstrap" in payload

        status, _, payload = authenticated_request(f"{server.url}/api/v1/auth/me")
        assert status == 401
        assert payload["code"] == "authentication_required"

        status, _, payload = authenticated_request(f"{server.url}/api/v1/admin/users")
        assert status == 401
        assert payload["code"] == "authentication_required"
    finally:
        server.stop()


def test_cli_accepts_setup_auth_mode():
    from app.server.__main__ import build_arg_parser

    args = build_arg_parser().parse_args(["--auth-mode", "setup"])
    assert args.auth_mode == "setup"
