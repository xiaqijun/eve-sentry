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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    store = IntelStore(args.data)
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
