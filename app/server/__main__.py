"""Run the EVE Sentry intel server as a standalone process."""

import argparse
import logging
import time
from typing import Any

from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the standalone intel server argument parser."""
    parser = argparse.ArgumentParser(description="Run the EVE Sentry intel server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--data", default="intel_reports.json")
    parser.add_argument("--storage", choices=["json", "sqlite"], default="sqlite")
    parser.add_argument("--db", default="intel.sqlite3")
    parser.add_argument("--config", default="intel_config.json")
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
    parser.add_argument("--enable-killboard", action="store_true")
    parser.add_argument("--zkill-cache", default="zkill_cache.json")
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
    killboard = None
    enable_esi = _should_enable_esi(args)
    if enable_esi or args.enable_killboard:
        from app.esi.cache import EsiCache

    if enable_esi:
        from app.esi.resolver import EsiResolver

        resolver = EsiResolver(cache=EsiCache(args.esi_cache))
        if args.esi_client_id:
            esi_session = _build_esi_session(args)

    if args.enable_killboard:
        from app.killboard.zkill_client import ZKillboardClient

        killboard = ZKillboardClient(cache=EsiCache(args.zkill_cache))

    enricher = None
    if resolver is not None or killboard is not None:
        from app.intel.enrichment import ThreatEnricher

        enricher = ThreatEnricher(
            resolver=resolver,
            killboard=killboard,
            esi_session=esi_session,
        )

    from app.intel.config import IntelConfigStore

    config_store = IntelConfigStore(args.config)
    scorer = config_store.build_scorer()

    store = _build_store(
        args,
        resolver=resolver,
        scorer=scorer,
        enricher=enricher,
    )
    server = IntelHTTPServer(
        store,
        host=args.host,
        port=args.port,
        config_store=config_store,
        esi_session=esi_session,
    )
    server.start()
    print(f"Intel map: {server.url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


def _build_store(
    args: argparse.Namespace,
    resolver: Any | None = None,
    scorer: Any | None = None,
    enricher: Any | None = None,
) -> IntelStore:
    if args.storage == "sqlite":
        from app.server.sqlite_store import SQLiteIntelStore

        return SQLiteIntelStore(
            args.db,
            import_json_path=args.data,
            resolver=resolver,
            scorer=scorer,
            enricher=enricher,
        )
    return IntelStore(
        args.data,
        resolver=resolver,
        scorer=scorer,
        enricher=enricher,
    )


def _validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if (args.esi_login or args.esi_login_only) and not args.esi_client_id.strip():
        parser.error("--esi-client-id is required when using ESI login")


def _should_enable_esi(args: argparse.Namespace) -> bool:
    return bool(args.enable_esi or (args.esi_login and not args.esi_login_only))


def _build_esi_sso_client(args: argparse.Namespace) -> Any:
    from app.esi.sso import DEFAULT_SCOPES, EveSsoClient

    return EveSsoClient(
        client_id=args.esi_client_id,
        redirect_uri=args.esi_redirect_uri,
        scopes=args.esi_scopes or DEFAULT_SCOPES,
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
