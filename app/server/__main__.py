"""Run the EVE Sentry intel server as a standalone process."""

import argparse
import logging
import time

from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the EVE Sentry intel server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--data", default="intel_reports.json")
    parser.add_argument("--storage", choices=["json", "sqlite"], default="json")
    parser.add_argument("--db", default="intel.sqlite3")
    parser.add_argument("--enable-esi", action="store_true")
    parser.add_argument("--esi-cache", default="esi_cache.json")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    resolver = None
    if args.enable_esi:
        from app.esi.cache import EsiCache
        from app.esi.resolver import EsiResolver

        resolver = EsiResolver(cache=EsiCache(args.esi_cache))

    if args.storage == "sqlite":
        from app.server.sqlite_store import SQLiteIntelStore

        store = SQLiteIntelStore(
            args.db,
            import_json_path=args.data,
            resolver=resolver,
        )
    else:
        store = IntelStore(args.data, resolver=resolver)
    server = IntelHTTPServer(store, host=args.host, port=args.port)
    server.start()
    print(f"Intel map: {server.url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
