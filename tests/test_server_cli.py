import pytest

from app.server import __main__ as server_main
from app.server.__main__ import build_arg_parser
from app.server.intel_store import IntelStore, StarSystem
from app.server.postgres_store import PostgreSQLIntelStore
from app.server.sqlite_store import SQLiteIntelStore


def test_server_cli_defaults_to_sqlite_storage():
    args = build_arg_parser().parse_args([])

    assert args.storage == "sqlite"
    assert args.db == "intel.sqlite3"
    assert args.data == "intel_reports.json"
    assert args.map_config == "intel_map.json"
    assert args.map_source is None
    assert args.map_region is None
    assert args.map_system is None
    assert args.map_sde_path is None
    assert args.map_refresh_on_start is False
    assert args.esi_client_id == ""
    assert args.auth_esi_client_id == ""
    assert args.auth_esi_redirect_uri == ""
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


def test_server_cli_accepts_postgres_storage_options():
    args = build_arg_parser().parse_args(
        [
            "--storage",
            "postgres",
            "--postgres-dsn",
            "postgresql://user:secret@example.test:5432/eve_sentry",
        ]
    )

    assert args.storage == "postgres"
    assert args.postgres_dsn == "postgresql://user:secret@example.test:5432/eve_sentry"


def test_server_cli_requires_postgres_dsn():
    parser = build_arg_parser()
    args = parser.parse_args(["--storage", "postgres"])

    with pytest.raises(SystemExit):
        server_main._validate_args(parser, args)


def test_server_cli_accepts_map_sde_options():
    args = build_arg_parser().parse_args(
        [
            "--map-source",
            "sde",
            "--map-config",
            "intel_map.json",
            "--map-region",
            "10000002",
            "--map-system",
            "30002813",
            "--map-sde-path",
            "sde",
            "--map-refresh-on-start",
        ]
    )

    assert args.map_source == "sde"
    assert args.map_config == "intel_map.json"
    assert args.map_region == [10000002]
    assert args.map_system == [30002813]
    assert args.map_sde_path == "sde"
    assert args.map_refresh_on_start is True


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


def test_server_cli_build_store_can_use_postgres_storage(monkeypatch):
    calls = []

    class DummyPostgresStore:
        def __init__(
            self,
            dsn,
            import_json_path=None,
            systems=None,
            links=None,
            resolver=None,
            scorer=None,
            enricher=None,
            allow_unmapped_systems=True,
        ):
            calls.append(
                {
                    "dsn": dsn,
                    "import_json_path": import_json_path,
                    "systems": systems,
                    "links": links,
                    "resolver": resolver,
                    "scorer": scorer,
                    "enricher": enricher,
                    "allow_unmapped_systems": allow_unmapped_systems,
                }
            )

    monkeypatch.setattr(
        "app.server.postgres_store.PostgreSQLIntelStore",
        DummyPostgresStore,
    )
    args = build_arg_parser().parse_args(
        [
            "--storage",
            "postgres",
            "--postgres-dsn",
            "postgresql://user:secret@example.test/eve_sentry",
            "--data",
            "legacy.json",
        ]
    )

    store = server_main._build_store(
        args,
        systems={"Tama": StarSystem("Tama", 1, 2)},
        links=[("Tama", "Kedama")],
        resolver="resolver",
        scorer="scorer",
        enricher="enricher",
    )

    assert isinstance(store, DummyPostgresStore)
    assert not isinstance(store, PostgreSQLIntelStore)
    assert calls == [
        {
            "dsn": "postgresql://user:secret@example.test/eve_sentry",
            "import_json_path": "legacy.json",
            "systems": {"Tama": StarSystem("Tama", 1, 2)},
            "links": [("Tama", "Kedama")],
            "resolver": "resolver",
            "scorer": "scorer",
            "enricher": "enricher",
            "allow_unmapped_systems": False,
        }
    ]


def test_server_cli_build_store_keeps_configured_map_locked(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--data", str(json_path)]
    )

    store = server_main._build_store(
        args,
        systems={"0-UVHJ": StarSystem("0-UVHJ", 100, 120, "Tenal")},
        links=[],
    )
    store.add_report("Jita", ["Alice"], seen_at="2099-01-01T00:00:00+00:00")

    snapshot = store.snapshot()

    assert [system["name"] for system in snapshot["systems"]] == ["0-UVHJ"]
    assert snapshot["reports"][0]["system_name"] == "Jita"


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


def test_server_cli_accepts_member_web_esi_login_options():
    args = build_arg_parser().parse_args(
        [
            "--auth-esi-client-id",
            "member-client-id",
            "--auth-esi-redirect-uri",
            "http://sentry.test/api/v1/auth/esi/callback",
        ]
    )

    assert args.auth_esi_client_id == "member-client-id"
    assert args.auth_esi_redirect_uri == "http://sentry.test/api/v1/auth/esi/callback"


def test_server_cli_builds_esi_config_summary(tmp_path):
    token_file = tmp_path / "esi_tokens.json"
    token_file.write_text("{}", encoding="utf-8")
    args = build_arg_parser().parse_args(
        [
            "--esi-client-id",
            "client-id",
            "--esi-token-file",
            str(token_file),
            "--esi-token-storage",
            "plain",
            "--esi-redirect-uri",
            "http://127.0.0.1:9000/callback",
            "--esi-scope",
            "esi-location.read_location.v1",
        ]
    )

    summary = server_main._build_esi_config(args)

    assert summary == {
        "client_id_configured": True,
        "redirect_uri": "http://127.0.0.1:9000/callback",
        "token_file": str(token_file),
        "token_file_present": True,
        "token_storage": "plain",
        "scopes": ["esi-location.read_location.v1"],
    }


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

        def __init__(
            self,
            store,
            host,
            port,
            config_store,
            esi_session,
            esi_config,
            esi_login,
            map_config_store,
        ):
            calls["server"].append(
                {
                    "store": store,
                    "host": host,
                    "port": port,
                        "config_store": config_store,
                        "esi_session": esi_session,
                        "esi_config": esi_config,
                        "esi_login": esi_login,
                        "map_config_store": map_config_store,
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

    def fake_build_store(
        args,
        systems=None,
        links=None,
        resolver=None,
        scorer=None,
        enricher=None,
    ):
        calls["build_store"].append(
            {
                "storage": args.storage,
                "db": args.db,
                "data": args.data,
                "systems": systems,
                "links": links,
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
    assert len(calls["build_store"]) == 1
    assert calls["build_store"][0]["storage"] == "sqlite"
    assert calls["build_store"][0]["db"] == "intel.sqlite3"
    assert calls["build_store"][0]["data"] == "intel_reports.json"
    assert calls["build_store"][0]["resolver"] is None
    assert calls["build_store"][0]["scorer"] == "dummy-scorer"
    assert calls["build_store"][0]["enricher"] is None
    assert calls["build_store"][0]["systems"]
    assert calls["build_store"][0]["links"]
    assert len(calls["server"]) == 1
    assert isinstance(calls["server"][0]["store"], DummyStore)
    assert calls["server"][0]["host"] == "127.0.0.1"
    assert calls["server"][0]["port"] == 8765
    assert calls["server"][0]["esi_session"] is None
    assert calls["server"][0]["esi_config"]["client_id_configured"] is False
    assert calls["server"][0]["esi_login"] is None
    assert calls["server"][0]["map_config_store"] is not None
