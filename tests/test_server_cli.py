import pytest

from app.server import __main__ as server_main
from app.server.__main__ import build_arg_parser
from app.server.intel_store import IntelStore
from app.server.sqlite_store import SQLiteIntelStore


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


def test_server_cli_default_store_uses_sqlite_and_imports_legacy_json(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    db_path = tmp_path / "intel.sqlite3"
    legacy = IntelStore(json_path, systems={}, links=[])
    legacy.add_report(
        "Tama",
        ["Alice"],
        source="ocr",
        seen_at="2026-06-29T12:00:00+00:00",
    )
    args = build_arg_parser().parse_args(
        ["--db", str(db_path), "--data", str(json_path)]
    )

    store = server_main._build_store(args)

    assert isinstance(store, SQLiteIntelStore)
    assert db_path.exists()
    assert [report["names"] for report in store.list_reports()] == [["Alice"]]


def test_server_cli_build_store_can_use_legacy_json_storage(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--data", str(json_path)]
    )

    store = server_main._build_store(args)
    report = store.add_report("Tama", ["Bob"], seen_at="2026-06-29T12:00:00+00:00")

    assert isinstance(store, IntelStore)
    assert not isinstance(store, SQLiteIntelStore)
    assert json_path.exists()
    assert [item["id"] for item in store.list_reports()] == [report.report_id]


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


def test_server_cli_main_starts_server_with_default_sqlite_store(monkeypatch):
    calls = {"build_store": [], "server": []}

    class DummyStore:
        pass

    class DummyServer:
        url = "http://127.0.0.1:8765"

        def __init__(self, store, host, port, config_store, esi_session):
            calls["server"].append(
                {
                    "store": store,
                    "host": host,
                    "port": port,
                    "config_store": config_store,
                    "esi_session": esi_session,
                }
            )

        def start(self):
            return None

        def stop(self):
            return None

    class DummyConfigStore:
        def __init__(self, path):
            self.path = path

        def build_scorer(self):
            return "dummy-scorer"

    def fake_build_store(args, resolver=None, scorer=None, enricher=None):
        calls["build_store"].append(
            {
                "storage": args.storage,
                "db": args.db,
                "data": args.data,
                "resolver": resolver,
                "scorer": scorer,
                "enricher": enricher,
            }
        )
        return DummyStore()

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(server_main, "_build_store", fake_build_store)
    monkeypatch.setattr(server_main, "IntelHTTPServer", DummyServer)
    monkeypatch.setattr(server_main.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        "app.intel.config.IntelConfigStore",
        DummyConfigStore,
    )

    code = server_main.main([])

    assert code == 0
    assert calls["build_store"] == [
        {
            "storage": "sqlite",
            "db": "intel.sqlite3",
            "data": "intel_reports.json",
            "resolver": None,
            "scorer": "dummy-scorer",
            "enricher": None,
        }
    ]
    assert len(calls["server"]) == 1
    assert isinstance(calls["server"][0]["store"], DummyStore)
    assert calls["server"][0]["host"] == "127.0.0.1"
    assert calls["server"][0]["port"] == 8765
    assert calls["server"][0]["esi_session"] is None
