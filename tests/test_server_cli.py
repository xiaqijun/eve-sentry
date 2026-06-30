from app.server.__main__ import build_arg_parser


def test_server_cli_defaults_to_sqlite_storage():
    args = build_arg_parser().parse_args([])

    assert args.storage == "sqlite"
    assert args.db == "intel.sqlite3"
    assert args.data == "intel_reports.json"
    assert args.esi_client_id == ""
    assert args.esi_redirect_uri == "http://127.0.0.1:8766/callback"
    assert args.esi_token_file == "esi_tokens.json"


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
            "--esi-redirect-uri",
            "http://127.0.0.1:9000/callback",
        ]
    )

    assert args.enable_esi is True
    assert args.esi_client_id == "client-id"
    assert args.esi_token_file == "tokens.json"
    assert args.esi_redirect_uri == "http://127.0.0.1:9000/callback"
