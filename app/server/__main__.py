"""Run the EVE Sentry intel server as a standalone process."""

import argparse
import logging
import signal
import threading
from pathlib import Path
from typing import Any

from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore
from app.server.map_config import MapConfigStore


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the standalone intel server argument parser."""
    parser = argparse.ArgumentParser(description="Run the intel server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--data", default="intel_reports.json")
    parser.add_argument(
        "--report-retention-days",
        type=int,
        default=0,
        help="delete reports older than this many days on startup; 0 disables",
    )
    parser.add_argument(
        "--inactive-intel-retention-days",
        type=int,
        default=30,
        help=(
            "delete inactive PostgreSQL intel rows older than this many days; "
            "0 disables"
        ),
    )
    parser.add_argument(
        "--storage",
        choices=["json", "postgres"],
        default="postgres",
    )
    parser.add_argument("--postgres-dsn", default="")
    parser.add_argument(
        "--hot-report-limit",
        type=int,
        default=5000,
        help="maximum recent reports held in memory by PostgreSQL storage",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["off", "setup", "enforce"],
        default="off",
    )
    parser.add_argument("--auth-bootstrap-admin", default="")
    parser.add_argument("--auth-bootstrap-password-file", default="")
    parser.add_argument(
        "--seat-integration-token",
        default="",
        help="Bearer token for the optional SeAT key-management integration",
    )
    parser.add_argument(
        "--seat-auth-mode",
        choices=["off", "enforce"],
        default="off",
        help="Authenticate and scope SeAT-issued keys independently of general auth",
    )
    parser.add_argument("--config", default="intel_config.json")
    parser.add_argument("--map-config", default="intel_map.json")
    parser.add_argument(
        "--map-source",
        choices=["builtin", "manual", "esi", "sde"],
        default=None,
    )
    parser.add_argument("--map-region", action="append", type=int, default=None)
    parser.add_argument("--map-system", action="append", type=int, default=None)
    parser.add_argument("--map-sde-path", default=None)
    parser.add_argument("--map-refresh-on-start", action="store_true")
    parser.add_argument("--enable-esi", action="store_true")
    parser.add_argument("--esi-cache", default="esi_cache.json")
    parser.add_argument(
        "--esi-backend",
        choices=["local", "remote"],
        default="local",
        help="public ESI backend; remote uses the private ESI Gateway",
    )
    parser.add_argument("--esi-gateway-url", default="")
    parser.add_argument("--esi-gateway-token", default="")
    parser.add_argument("--esi-remote-timeout", type=float, default=8.0)
    parser.add_argument(
        "--esi-no-local-fallback",
        action="store_true",
        help="do not fall back to direct public ESI when the Gateway fails",
    )
    parser.add_argument(
        "--enable-killboard",
        action="store_true",
        help=argparse.SUPPRESS,  # Retired; accepted for old deployment scripts.
    )
    parser.add_argument(
        "--disable-killboard",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--zkill-cache", default="zkill_cache.json", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    resolver = None
    esi_cache = None
    enable_esi = _should_enable_esi(args)
    if enable_esi:
        from app.esi.cache import EsiCache
        esi_cache = EsiCache(args.esi_cache)

    if enable_esi:
        from app.esi.resolver import EsiResolver
        resolver_client = _build_public_esi_client(args)

        resolver = EsiResolver(client=resolver_client, cache=esi_cache)

    enricher = None
    if resolver is not None:
        from app.intel.enrichment import ThreatEnricher

        enricher = ThreatEnricher(
            resolver=resolver,
        )

    from app.intel.config import IntelConfigStore

    config_store = IntelConfigStore(args.config)
    map_config_store = MapConfigStore(args.map_config)
    from app.server.map_settings import restore_monitoring_config

    saved_monitoring = restore_monitoring_config(map_config_store)
    map_overrides: dict[str, Any] = {}
    if args.map_source is not None and not saved_monitoring:
        map_overrides["source"] = args.map_source
    if args.map_region is not None and not saved_monitoring:
        map_overrides["region_ids"] = args.map_region
    if args.map_system is not None and not saved_monitoring:
        map_overrides["system_ids"] = args.map_system
    if args.map_sde_path is not None:
        map_overrides["sde_path"] = args.map_sde_path
    if map_overrides:
        map_config_store.update(map_overrides)
    scorer = config_store.build_scorer()
    systems, links = map_config_store.build_map(
        resolver=resolver,
        refresh_if_needed=args.map_refresh_on_start,
    )

    store = _build_store(
        args,
        systems=systems,
        links=links,
        resolver=resolver,
        scorer=scorer,
        enricher=enricher,
    )
    auth_service = _build_auth_service(args, store, resolver)
    server_options = {
        "host": args.host,
        "port": args.port,
        "config_store": config_store,
        "esi_config": _build_esi_config(args),
        "map_config_store": map_config_store,
    }
    if args.seat_integration_token:
        server_options["seat_integration_token"] = args.seat_integration_token
    if auth_service is not None:
        server_options["auth_service"] = auth_service
    server = IntelHTTPServer(store, **server_options)
    started = False
    try:
        from app.esi.personnel_setup import configure_personnel
        from app.server.personnel_settings import PersonnelSettings
        personnel_settings = PersonnelSettings(store, args, resolver, auth_service)
        configure_personnel(store, args, resolver, configuration=personnel_settings.startup_values())
        store._personnel_settings = personnel_settings
        server.start()
        started = True
        print(f"Intel map: {server.url}")
        _wait_for_shutdown()
    finally:
        try:
            if started:
                server.stop()
        finally:
            try:
                close_auth = getattr(auth_service, "close", None)
                if callable(close_auth):
                    close_auth()
            finally:
                close_store = getattr(store, "close", None)
                if callable(close_store):
                    close_store()
    return 0


def _wait_for_shutdown() -> None:
    """Wait for SIGINT or SIGTERM while restoring the caller's handlers."""
    shutdown_requested = threading.Event()

    def request_shutdown(signum, _frame) -> None:
        logger = logging.getLogger(__name__)
        logger.info("Shutdown requested by signal %s", signum)
        shutdown_requested.set()

    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for signum in previous_handlers:
            signal.signal(signum, request_shutdown)
        shutdown_requested.wait()
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _build_store(
    args: argparse.Namespace,
    systems: dict[str, Any] | None = None,
    links: list[tuple[str, str]] | None = None,
    resolver: Any | None = None,
    scorer: Any | None = None,
    enricher: Any | None = None,
) -> IntelStore:
    if args.storage == "postgres":
        from app.server.postgres_store import PostgreSQLIntelStore

        store = PostgreSQLIntelStore(
            args.postgres_dsn,
            import_json_path=args.data,
            systems=systems,
            links=links,
            resolver=resolver,
            scorer=scorer,
            enricher=enricher,
            allow_unmapped_systems=False,
            hot_report_limit=args.hot_report_limit,
        )
    else:
        store = IntelStore(
            args.data,
            systems=systems,
            links=links,
            resolver=resolver,
            scorer=scorer,
            enricher=enricher,
            allow_unmapped_systems=False,
        )

    report_retention_days = int(
        getattr(args, "report_retention_days", 0) or 0
    )
    inactive_intel_retention_days = int(
        getattr(args, "inactive_intel_retention_days", 30) or 0
    )
    try:
        if args.storage == "postgres" and inactive_intel_retention_days > 0:
            removed = store.prune_inactive_active_intel_older_than(
                inactive_intel_retention_days
            )
            logging.getLogger(__name__).info(
                "Pruned %s inactive intel rows older than %s days",
                removed,
                inactive_intel_retention_days,
            )
        if report_retention_days > 0:
            removed = store.prune_reports_older_than(report_retention_days)
            logging.getLogger(__name__).info(
                "Pruned %s reports older than %s days",
                removed,
                report_retention_days,
            )
    except Exception:
        try:
            store.close()
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to close store after startup pruning failed"
            )
        raise
    return store


def _validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.storage == "postgres" and not str(args.postgres_dsn or "").strip():
        parser.error("--postgres-dsn is required when using PostgreSQL storage")
    if args.report_retention_days < 0:
        parser.error("--report-retention-days must not be negative")
    if args.inactive_intel_retention_days < 0:
        parser.error("--inactive-intel-retention-days must not be negative")
    if args.hot_report_limit <= 0:
        parser.error("--hot-report-limit must be positive")
    if args.auth_mode != "off" and args.storage == "json":
        parser.error("authentication requires PostgreSQL storage")
    if args.auth_bootstrap_admin and not args.auth_bootstrap_password_file:
        parser.error(
            "--auth-bootstrap-password-file is required with --auth-bootstrap-admin"
        )
    if args.seat_integration_token and len(args.seat_integration_token) < 32:
        parser.error("--seat-integration-token must be at least 32 characters")
    if args.esi_backend == "remote":
        if not str(args.esi_gateway_url or "").strip():
            parser.error("--esi-gateway-url is required with --esi-backend remote")
        if len(str(args.esi_gateway_token or "").strip()) < 32:
            parser.error("--esi-gateway-token must be at least 32 characters")
        if args.esi_remote_timeout <= 0:
            parser.error("--esi-remote-timeout must be positive")


def _should_enable_esi(args: argparse.Namespace) -> bool:
    return bool(args.enable_esi)


def _build_auth_service(
    args: argparse.Namespace,
    store: IntelStore,
    resolver: Any | None,
) -> Any | None:
    if args.auth_mode == "off" and args.seat_auth_mode == "off":
        return None
    connect = getattr(store, "_connect", None)
    if not callable(connect):
        raise RuntimeError("authentication requires a SQL-backed store")
    from app.server.auth import AuthService
    from app.server.auth_store import AuthRepository

    service = AuthService(
        AuthRepository(connect),
        resolver,
        enforce_requests=args.auth_mode == "enforce",
        seat_auth_mode=args.seat_auth_mode,
    )
    username = str(args.auth_bootstrap_admin or "").strip()
    if username:
        password_path = Path(args.auth_bootstrap_password_file)
        try:
            password = password_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"could not read bootstrap admin password file: {password_path}"
            ) from exc
        service.ensure_bootstrap_admin(username, password)
    if service.repository.count_users() == 0:
        raise RuntimeError(
            "authentication has no users; configure --auth-bootstrap-admin and "
            "--auth-bootstrap-password-file for the first start"
        )
    return service


def _build_esi_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "backend": str(getattr(args, "esi_backend", "local") or "local"),
        "gateway_url": str(getattr(args, "esi_gateway_url", "") or "").strip(),
        "local_fallback": not bool(getattr(args, "esi_no_local_fallback", False)),
        "authenticated_esi_enabled": False,
    }


def _build_public_esi_client(args: argparse.Namespace) -> Any:
    """Build the explicit transport, or retain the legacy public JSON gateway."""
    from app.esi.transport import configured_client

    transport = configured_client(args)
    if transport is not None:
        return transport
    if str(getattr(args, "esi_backend", "local") or "local") != "remote":
        from app.esi.client import EsiClient

        return EsiClient()
    from app.esi.client import EsiClient
    from app.esi.remote import RemoteEsiClient

    fallback = None if getattr(args, "esi_no_local_fallback", False) else EsiClient()
    return RemoteEsiClient(
        args.esi_gateway_url,
        args.esi_gateway_token,
        timeout=args.esi_remote_timeout,
        fallback=fallback,
    )


if __name__ == "__main__":
    raise SystemExit(main())
