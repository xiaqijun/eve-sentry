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
    parser.add_argument(
        "--key-risk-control",
        choices=["on", "off"],
        default="on",
        help="enable ESI-backed desktop-key identity risk control",
    )
    parser.add_argument("--auth-bootstrap-admin", default="")
    parser.add_argument("--auth-bootstrap-password-file", default="")
    parser.add_argument("--auth-esi-client-id", default="")
    parser.add_argument("--auth-esi-redirect-uri", default="")
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
    parser.add_argument("--esi-client-id", default="")
    parser.add_argument(
        "--esi-redirect-uri",
        default="http://127.0.0.1:8766/callback",
    )
    parser.add_argument("--esi-token-file", default="esi_tokens.json")
    parser.add_argument(
        "--esi-token-storage",
        choices=["auto", "secure", "plain"],
        default="auto",
        help="ESI token storage protection mode",
    )
    parser.add_argument(
        "--esi-login",
        action="store_true",
        help="complete local EVE SSO authorization before starting the server",
    )
    parser.add_argument(
        "--esi-login-only",
        action="store_true",
        help="complete local EVE SSO authorization, save tokens, and exit",
    )
    parser.add_argument("--esi-login-timeout", type=float, default=300.0)
    parser.add_argument("--esi-no-browser", action="store_true")
    parser.add_argument("--esi-scope", action="append", default=[], dest="esi_scopes")
    parser.add_argument(
        "--enable-killboard",
        action="store_true",
        help="enable zKillboard character statistics enrichment",
    )
    parser.add_argument(
        "--disable-killboard",
        action="store_true",
        help="disable zKillboard enrichment even when public ESI is enabled",
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

    if args.esi_login or args.esi_login_only:
        _run_esi_login(args)
        if args.esi_login_only:
            return 0

    resolver = None
    esi_session = None
    esi_login = None
    enable_esi = _should_enable_esi(args)
    if enable_esi:
        from app.esi.cache import EsiCache

    if enable_esi:
        from app.esi.resolver import EsiResolver

        resolver = EsiResolver(cache=EsiCache(args.esi_cache))
        if args.esi_client_id:
            esi_session = _build_esi_session(args)
            esi_login = _build_esi_login(args)

    enricher = None
    if resolver is not None or esi_session is not None:
        from app.intel.enrichment import ThreatEnricher

        killboard = None
        if not args.disable_killboard and (args.enable_killboard or resolver is not None):
            from app.intel.zkillboard import ZkillboardClient

            killboard = ZkillboardClient()

        enricher = ThreatEnricher(
            resolver=resolver,
            killboard=killboard,
            esi_session=esi_session,
        )

    from app.intel.config import IntelConfigStore

    config_store = IntelConfigStore(args.config)
    map_config_store = MapConfigStore(args.map_config)
    map_overrides: dict[str, Any] = {}
    if args.map_source is not None:
        map_overrides["source"] = args.map_source
    if args.map_region is not None:
        map_overrides["region_ids"] = args.map_region
    if args.map_system is not None:
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
        "esi_session": esi_session,
        "esi_config": _build_esi_config(args),
        "esi_login": esi_login,
        "map_config_store": map_config_store,
    }
    if auth_service is not None:
        server_options["auth_service"] = auth_service
    server = IntelHTTPServer(store, **server_options)
    started = False
    try:
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
    if (args.esi_login or args.esi_login_only) and not args.esi_client_id.strip():
        parser.error("--esi-client-id is required when using ESI login")
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


def _should_enable_esi(args: argparse.Namespace) -> bool:
    return bool(
        args.enable_esi
        or args.auth_mode != "off"
        or (args.esi_login and not args.esi_login_only)
    )


def _build_auth_service(
    args: argparse.Namespace,
    store: IntelStore,
    resolver: Any | None,
) -> Any | None:
    key_risk_control = str(getattr(args, "key_risk_control", "on") or "on")
    if args.auth_mode == "off":
        return None
    connect = getattr(store, "_connect", None)
    if not callable(connect):
        raise RuntimeError("authentication requires a SQL-backed store")
    if resolver is None and key_risk_control == "on":
        raise RuntimeError("key risk control requires public ESI")

    from app.server.auth import AuthService
    from app.server.auth_store import AuthRepository

    service = AuthService(
        AuthRepository(connect),
        resolver,
        enforce_requests=args.auth_mode == "enforce",
        esi_sso_client=_build_auth_esi_sso_client(args),
        key_risk_control=key_risk_control == "on",
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
    service.start_identity_worker()
    return service


def _build_esi_sso_client(args: argparse.Namespace) -> Any:
    from app.esi.sso import DEFAULT_SCOPES, EveSsoClient

    return EveSsoClient(
        client_id=args.esi_client_id,
        redirect_uri=args.esi_redirect_uri,
        scopes=args.esi_scopes or DEFAULT_SCOPES,
    )


def _build_auth_esi_sso_client(args: argparse.Namespace) -> Any | None:
    client_id = str(args.esi_client_id or "").strip()
    redirect_uri = str(args.esi_redirect_uri or "").strip()
    if not client_id or not redirect_uri:
        return None
    from app.esi.sso import EveSsoClient

    return EveSsoClient(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=[],
    )


def _build_esi_session(args: argparse.Namespace) -> Any:
    from app.esi.session import EsiAuthenticatedSession
    from app.esi.sso import build_token_store

    return EsiAuthenticatedSession(
        sso_client=_build_esi_sso_client(args),
        token_store=build_token_store(
            args.esi_token_file,
            storage=args.esi_token_storage,
        ),
    )


def _build_esi_login(args: argparse.Namespace) -> Any:
    from app.esi.sso import EsiLoginManager, build_token_store

    return EsiLoginManager(
        client=_build_esi_sso_client(args),
        token_store=build_token_store(
            args.esi_token_file,
            storage=args.esi_token_storage,
        ),
        timeout_seconds=args.esi_login_timeout,
    )


def _build_esi_config(args: argparse.Namespace) -> dict[str, Any]:
    token_file = str(args.esi_token_file or "").strip()
    token_path = Path(token_file) if token_file else None
    return {
        "client_id_configured": bool(str(args.esi_client_id or "").strip()),
        "redirect_uri": str(args.esi_redirect_uri or "").strip(),
        "token_file": token_file,
        "token_file_present": bool(token_path and token_path.exists()),
        "token_storage": str(args.esi_token_storage or "").strip(),
        "scopes": list(args.esi_scopes or []),
    }


def _run_esi_login(args: argparse.Namespace) -> Any:
    from app.esi.sso import build_token_store, run_local_sso_login

    token_store = build_token_store(
        args.esi_token_file,
        storage=args.esi_token_storage,
    )
    tokens = run_local_sso_login(
        _build_esi_sso_client(args),
        token_store,
        timeout_seconds=args.esi_login_timeout,
        open_browser=not args.esi_no_browser,
        announce_url=lambda url: print(f"Open this URL to authorize:\n{url}"),
    )
    character = tokens.character_id or "unknown"
    storage = "secure" if token_store.is_secure else "plain"
    print(
        f"Saved ESI token for character {character} to {token_store.path} "
        f"({storage} storage)"
    )
    return tokens


if __name__ == "__main__":
    raise SystemExit(main())
