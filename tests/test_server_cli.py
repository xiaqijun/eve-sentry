import pytest

from app.server import __main__ as server_main
from app.server.__main__ import build_arg_parser
from app.server.intel_store import IntelStore, StarSystem
from app.server.postgres_store import PostgreSQLIntelStore
from scripts.run_server import build_server_argv


def test_server_cli_defaults_to_postgres_storage():
    args = build_arg_parser().parse_args([])

    assert args.storage == "postgres"
    assert args.postgres_dsn == ""
    assert args.hot_report_limit == 5000
    assert args.data == "intel_reports.json"
    assert args.map_config == "intel_map.json"
    assert args.map_source is None
    assert args.map_region is None
    assert args.map_system is None
    assert args.map_sde_path is None
    assert args.map_refresh_on_start is False
    assert args.enable_esi is False
    assert args.esi_cache == "esi_cache.json"
    assert args.esi_backend == "local"
    assert args.seat_auth_mode == "off"


@pytest.mark.parametrize("option", ["--esi-login", "--esi-login-only", "--esi-client-id"])
def test_server_cli_rejects_retired_eve_oauth_options(option):
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args([option, "value"] if option == "--esi-client-id" else [option])


def test_server_cli_can_select_json_storage():
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


def test_server_cli_validates_optional_seat_integration_token():
    parser = build_arg_parser()
    valid = parser.parse_args(["--seat-integration-token", "s" * 32])
    assert valid.seat_integration_token == "s" * 32
    invalid = parser.parse_args(["--seat-integration-token", "short"])
    with pytest.raises(SystemExit):
        server_main._validate_args(parser, invalid)


def test_server_cli_accepts_seat_auth_modes_and_env_binding():
    parser = build_arg_parser()
    assert parser.parse_args(["--seat-auth-mode", "enforce"]).seat_auth_mode == "enforce"
    with pytest.raises(SystemExit):
        parser.parse_args(["--seat-auth-mode", "shadow"])
    argv = build_server_argv({"EVE_SENTRY_SERVER_SEAT_AUTH_MODE": "enforce"})
    assert argv[-2:] == ["--seat-auth-mode", "enforce"]


def test_server_env_binds_seat_integration_token():
    argv = build_server_argv(
        {"EVE_SENTRY_SERVER_SEAT_INTEGRATION_TOKEN": "s" * 32}
    )
    assert argv[-2:] == ["--seat-integration-token", "s" * 32]


def test_server_cli_requires_postgres_dsn():
    parser = build_arg_parser()
    args = parser.parse_args(["--storage", "postgres"])

    with pytest.raises(SystemExit):
        server_main._validate_args(parser, args)


def test_server_cli_report_retention_defaults_off_and_rejects_negative_values():
    parser = build_arg_parser()
    defaults = parser.parse_args([])
    invalid = parser.parse_args(["--report-retention-days", "-1"])

    assert defaults.report_retention_days == 0
    with pytest.raises(SystemExit):
        server_main._validate_args(parser, invalid)


def test_server_cli_inactive_intel_retention_defaults_on_and_rejects_negative():
    parser = build_arg_parser()
    defaults = parser.parse_args([])
    disabled = parser.parse_args(["--inactive-intel-retention-days", "0"])
    invalid = parser.parse_args(["--inactive-intel-retention-days", "-1"])

    assert defaults.inactive_intel_retention_days == 30
    assert disabled.inactive_intel_retention_days == 0
    with pytest.raises(SystemExit):
        server_main._validate_args(parser, invalid)


def test_server_cli_prunes_reports_on_startup_when_explicitly_enabled(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    seed = IntelStore(json_path)
    old = seed.add_observation(
        {
            "source": "manual",
            "system_name": "Tama",
            "names": ["Old Pilot"],
            "seen_at": "2000-01-01T00:00:00+00:00",
            "received_at": "2000-01-01T00:00:00+00:00",
        }
    )
    recent = seed.add_observation(
        {
            "source": "manual",
            "system_name": "Tama",
            "names": ["Recent Pilot"],
            "seen_at": "2099-01-01T00:00:00+00:00",
            "received_at": "2099-01-01T00:00:00+00:00",
        }
    )
    seed.close()
    args = build_arg_parser().parse_args(
        [
            "--storage",
            "json",
            "--data",
            str(json_path),
            "--report-retention-days",
            "30",
        ]
    )

    store = server_main._build_store(args)
    try:
        assert [item["id"] for item in store.list_reports()] == [
            recent.observation_id
        ]
        assert old.observation_id not in {
            item["id"] for item in store.list_reports()
        }
    finally:
        store.close()


def test_server_cli_closes_store_when_startup_retention_fails(monkeypatch):
    calls = []

    class FailingStore:
        def __init__(self, *args, **kwargs):
            pass

        def prune_reports_older_than(self, retention_days):
            calls.append(("prune", retention_days))
            raise RuntimeError("pruning failed")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(server_main, "IntelStore", FailingStore)
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--report-retention-days", "30"]
    )

    with pytest.raises(RuntimeError, match="pruning failed"):
        server_main._build_store(args)

    assert calls == [("prune", 30), ("close",)]


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


def test_server_cli_json_store_uses_existing_data(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    legacy = IntelStore(json_path, systems={}, links=[])
    legacy.add_report(
        "Tama",
        ["Alice"],
        source="ocr",
        seen_at="2026-06-29T12:00:00+00:00",
    )
    legacy.close()
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--data", str(json_path)]
    )

    store = server_main._build_store(args)
    try:
        assert isinstance(store, IntelStore)
        assert [report["names"] for report in store.list_reports()] == [["Alice"]]
    finally:
        store.close()


def test_server_cli_build_store_can_use_legacy_json_storage(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    args = build_arg_parser().parse_args(
        ["--storage", "json", "--data", str(json_path)]
    )

    store = server_main._build_store(args)
    report = store.add_report("Tama", ["Bob"], seen_at="2026-06-29T12:00:00+00:00")

    assert isinstance(store, IntelStore)
    assert json_path.exists()
    assert [item["id"] for item in store.list_reports()] == [report.report_id]


def test_server_cli_build_store_can_use_postgres_storage(monkeypatch):
    calls = []
    prunes = []

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
            hot_report_limit=5000,
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
                    "hot_report_limit": hot_report_limit,
                }
            )

        def prune_inactive_active_intel_older_than(self, retention_days):
            prunes.append(retention_days)
            return 0

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
            "hot_report_limit": 5000,
        }
    ]
    assert prunes == [30]


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


def test_server_cli_builds_public_esi_config_summary():
    args = build_arg_parser().parse_args(
        ["--enable-esi", "--esi-backend", "remote", "--esi-gateway-url", "http://gateway.test", "--esi-gateway-token", "x" * 32]
    )

    assert server_main._build_esi_config(args) == {
        "backend": "remote",
        "gateway_url": "http://gateway.test",
        "local_fallback": True,
        "authenticated_esi_enabled": False,
    }


@pytest.mark.skip(reason="key risk control option was retired")
def test_server_cli_auth_mode_does_not_enable_authenticated_esi():
    args = build_arg_parser().parse_args(["--auth-mode", "setup", "--key-risk-control", "off"])

    assert args.key_risk_control == "off"
    assert server_main._should_enable_esi(args) is True


def test_server_cli_remote_esi_requires_gateway_credentials():
    parser = build_arg_parser()
    missing_url = parser.parse_args(
        ["--esi-backend", "remote", "--esi-gateway-token", "x" * 32]
    )
    with pytest.raises(SystemExit):
        server_main._validate_args(parser, missing_url)

    valid = parser.parse_args(
        [
            "--storage",
            "json",
            "--esi-backend",
            "remote",
            "--esi-gateway-url",
            "http://10.233.53.17:8787",
            "--esi-gateway-token",
            "x" * 32,
        ]
    )
    server_main._validate_args(parser, valid)


@pytest.mark.skip(reason="key risk control option was retired")
def test_server_cli_can_disable_key_risk_control_while_auth_keeps_esi_available():
    args = build_arg_parser().parse_args(
        ["--auth-mode", "setup", "--key-risk-control", "off"]
    )

    assert args.key_risk_control == "off"
    assert server_main._should_enable_esi(args) is True


def test_server_cli_main_starts_server_with_default_postgres_store(monkeypatch):
    calls = {"build_store": [], "server": [], "lifecycle": []}

    class DummyStore:
        def close(self):
            calls["lifecycle"].append("store.close")

    class DummyServer:
        url = "http://127.0.0.1:8765"

        def __init__(
            self,
            store,
            host,
            port,
            config_store,
            esi_config,
            map_config_store,
        ):
            calls["server"].append(
                {
                    "store": store,
                    "host": host,
                    "port": port,
                        "config_store": config_store,
                        "esi_config": esi_config,
                        "map_config_store": map_config_store,
                    }
                )

        def start(self):
            calls["lifecycle"].append("server.start")

        def stop(self):
            calls["lifecycle"].append("server.stop")

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
                "data": args.data,
                "systems": systems,
                "links": links,
                "resolver": resolver,
                "scorer": scorer,
                "enricher": enricher,
            }
        )
        return DummyStore()

    def fake_wait_for_shutdown():
        calls["lifecycle"].append("wait")

    monkeypatch.setattr(server_main, "_build_store", fake_build_store)
    monkeypatch.setattr(server_main, "IntelHTTPServer", DummyServer)
    monkeypatch.setattr(server_main, "_wait_for_shutdown", fake_wait_for_shutdown)
    monkeypatch.setattr(
        "app.intel.config.IntelConfigStore",
        DummyConfigStore,
    )

    code = server_main.main(
        ["--postgres-dsn", "postgresql://example.test/eve_sentry"]
    )

    assert code == 0
    assert len(calls["build_store"]) == 1
    assert calls["build_store"][0]["storage"] == "postgres"
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
    assert calls["server"][0]["esi_config"]["authenticated_esi_enabled"] is False
    assert calls["server"][0]["map_config_store"] is not None
    assert calls["lifecycle"] == [
        "server.start",
        "wait",
        "server.stop",
        "store.close",
    ]


def test_wait_for_shutdown_handles_sigterm_and_restores_handlers(monkeypatch):
    installed = {}
    calls = []
    previous_handlers = {
        server_main.signal.SIGINT: object(),
        server_main.signal.SIGTERM: object(),
    }

    class FakeEvent:
        def __init__(self):
            self.was_set = False

        def set(self):
            self.was_set = True

        def wait(self):
            installed[server_main.signal.SIGTERM](server_main.signal.SIGTERM, None)
            assert self.was_set is True

    def fake_signal(signum, handler):
        calls.append((signum, handler))
        installed[signum] = handler

    monkeypatch.setattr(server_main.threading, "Event", FakeEvent)
    monkeypatch.setattr(
        server_main.signal,
        "getsignal",
        lambda signum: previous_handlers[signum],
    )
    monkeypatch.setattr(server_main.signal, "signal", fake_signal)

    server_main._wait_for_shutdown()

    assert installed == previous_handlers
    assert [signum for signum, _ in calls] == [
        server_main.signal.SIGINT,
        server_main.signal.SIGTERM,
        server_main.signal.SIGINT,
        server_main.signal.SIGTERM,
    ]
