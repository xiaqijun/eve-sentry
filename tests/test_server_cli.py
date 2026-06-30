from app.server.__main__ import build_arg_parser


def test_server_cli_defaults_to_sqlite_storage():
    args = build_arg_parser().parse_args([])

    assert args.storage == "sqlite"
    assert args.db == "intel.sqlite3"
    assert args.data == "intel_reports.json"


def test_server_cli_can_select_legacy_json_storage():
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--data", "legacy.json"]
    )

    assert args.storage == "json"
    assert args.data == "legacy.json"
