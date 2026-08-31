#!/usr/bin/env python3
"""CLI entry point for the standalone ESI Gateway."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esi_gateway.cache import TtlCache
from esi_gateway.server import GatewayServer, GatewayState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the private ESI Gateway")
    parser.add_argument("--host", default=os.environ.get("EVE_SENTRY_ESI_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EVE_SENTRY_ESI_GATEWAY_PORT", "8787")))
    parser.add_argument("--token", default=os.environ.get("EVE_SENTRY_ESI_GATEWAY_TOKEN", ""))
    parser.add_argument("--allowed-client", action="append", default=None)
    parser.add_argument("--cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CACHE_TTL", "86400")))
    parser.add_argument("--cache-max-entries", type=int, default=int(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CACHE_MAX_ENTRIES", "4096")))
    parser.add_argument("--negative-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_NEGATIVE_TTL", "30")))
    parser.add_argument("--stale-grace", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_STALE_GRACE", "300")))
    parser.add_argument("--rate", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_RATE", "2")))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = str(args.token or "").strip()
    if len(token) < 32:
        raise SystemExit("--token or EVE_SENTRY_ESI_GATEWAY_TOKEN must be at least 32 characters")
    allowed = set(args.allowed_client or os.environ.get("EVE_SENTRY_ESI_GATEWAY_ALLOWED_CLIENTS", "").replace(",", " ").split())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = GatewayState(token, allowed, args.cache_ttl, args.rate, max_cache_entries=max(1, args.cache_max_entries), negative_ttl=args.negative_ttl, stale_grace=args.stale_grace)
    server = GatewayServer((args.host, args.port), state)
    logging.getLogger("esi_gateway").info("listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
