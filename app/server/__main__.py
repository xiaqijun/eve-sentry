"""Run the EVE Sentry intel server as a standalone process."""

import argparse
import logging
import time

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
    parser.add_argument("--enable-killboard", action="store_true")
    parser.add_argument("--zkill-cache", default="zkill_cache.json")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    resolver = None
    killboard = None
    if args.enable_esi or args.enable_killboard:
        from app.esi.cache import EsiCache

    if args.enable_esi:
        from app.esi.resolver import EsiResolver

        resolver = EsiResolver(cache=EsiCache(args.esi_cache))

    if args.enable_killboard:
        from app.killboard.zkill_client import ZKillboardClient

        killboard = ZKillboardClient(cache=EsiCache(args.zkill_cache))

    enricher = None
    if resolver is not None or killboard is not None:
        from app.intel.enrichment import ThreatEnricher

        enricher = ThreatEnricher(resolver=resolver, killboard=killboard)

    from app.intel.config import IntelConfigStore

    config_store = IntelConfigStore(args.config)
    scorer = config_store.build_scorer()

    if args.storage == "sqlite":
        from app.server.sqlite_store import SQLiteIntelStore

        store = SQLiteIntelStore(
            args.db,
            import_json_path=args.data,
            resolver=resolver,
            scorer=scorer,
            enricher=enricher,
        )
    else:
        store = IntelStore(
            args.data,
            resolver=resolver,
            scorer=scorer,
            enricher=enricher,
        )
    server = IntelHTTPServer(
        store,
        host=args.host,
        port=args.port,
        config_store=config_store,
    )
    server.start()
    print(f"Intel map: {server.url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
