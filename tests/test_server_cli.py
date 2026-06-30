import pytest

from app.server import __main__ as server_main
from app.server.__main__ import build_arg_parser


def test_server_cli_defaults_to_sqlite_storage():
    args = build_arg_parser().parse_args([])

    assert args.storage == "sqlite"
    assert args.db == "intel.sqlite3"
    assert args.data == "intel_reports.json"
    assert args.esi_client_id == ""
    assert args.esi_redirect_uri == "http://127.0.0.1:8766/callback"
    assert args.esi_token_file == "esi_tokens.json"
    assert args.esi_token_storage == "auto"
    assert args.esi_login is False
    assert args.esi_login_only is False
    assert args.esi_login_timeout == 300.0
    assert args.esi_no_browser is False
    assert args.esi_scopes == []


def test_server_cli_can_select_legacy_json_storage():
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--data", "legacy.json"]
    )

    assert args.storage == "json"
    assert args.data == "legacy.json"


def test_server_cli_accepts_authenticated_esi_options():
    args = build_arg_parser().parse_args(
        [
            "--enable-esi",
            "--esi-client-id",
            "client-id",
            "--esi-token-file",
            "tokens.json",
            "--esi-token-storage",
            "plain",
            "--esi-redirect-uri",
            "http://127.0.0.1:9000/callback",
            "--esi-login",
            "--esi-login-timeout",
            "10",
            "--esi-no-browser",
            "--esi-scope",
            "esi-location.read_location.v1",
        ]
    )

    assert args.enable_esi is True
    assert args.esi_client_id == "client-id"
    assert args.esi_token_file == "tokens.json"
    assert args.esi_token_storage == "plain"
    assert args.esi_redirect_uri == "http://127.0.0.1:9000/callback"
    assert args.esi_login is True
    assert args.esi_login_timeout == 10
    assert args.esi_no_browser is True
    assert args.esi_scopes == ["esi-location.read_location.v1"]


def test_server_cli_requires_client_id_for_login():
    parser = build_arg_parser()
    args = parser.parse_args(["--esi-login-only"])

    with pytest.raises(SystemExit):
        server_main._validate_args(parser, args)


def test_server_cli_login_implies_esi_for_server_start():
    args = build_arg_parser().parse_args(
        ["--esi-login", "--esi-client-id", "client-id"]
    )

    assert server_main._should_enable_esi(args) is True


def test_server_cli_login_only_runs_login_and_exits(monkeypatch):
    calls = []

    def fake_login(args):
        calls.append(args.esi_client_id)

    monkeypatch.setattr(server_main, "_run_esi_login", fake_login)

    code = server_main.main(
        ["--esi-login-only", "--esi-client-id", "client-id", "--esi-no-browser"]
    )

    assert code == 0
    assert calls == ["client-id"]
