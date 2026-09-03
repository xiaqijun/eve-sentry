#!/usr/bin/env python3
"""CLI entry point for the standalone ESI Gateway."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from esi_gateway.server import GatewayServer, GatewayState
from esi_gateway.id_cache import IdCacheCoordinator, MemoryStore, PostgresStore, RedisHotStore


SECONDS_PER_DAY = 24 * 60 * 60


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
    parser.add_argument("--postgres-dsn", default=os.environ.get("EVE_SENTRY_ESI_GATEWAY_POSTGRES_DSN", ""))
    parser.add_argument("--redis-url", default=os.environ.get("EVE_SENTRY_ESI_GATEWAY_REDIS_URL", ""))
    parser.add_argument("--id-cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_ID_CACHE_TTL", str(30 * SECONDS_PER_DAY))))
    parser.add_argument("--character-cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CHARACTER_CACHE_TTL", str(2 * SECONDS_PER_DAY))))
    parser.add_argument("--affiliation-cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_AFFILIATION_CACHE_TTL", "3600")))
    parser.add_argument("--corporation-cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CORPORATION_CACHE_TTL", str(7 * SECONDS_PER_DAY))))
    parser.add_argument("--alliance-cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_ALLIANCE_CACHE_TTL", str(7 * SECONDS_PER_DAY))))
    parser.add_argument("--system-cache-ttl", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_SYSTEM_CACHE_TTL", str(30 * SECONDS_PER_DAY))))
    parser.add_argument("--refresh-interval", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_REFRESH_INTERVAL", "5")))
    parser.add_argument("--refresh-batch-size", type=int, default=int(os.environ.get("EVE_SENTRY_ESI_GATEWAY_REFRESH_BATCH_SIZE", "1000")))
    parser.add_argument("--cache-retry-base", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CACHE_RETRY_BASE", "5")))
    parser.add_argument("--cache-retry-max", type=float, default=float(os.environ.get("EVE_SENTRY_ESI_GATEWAY_CACHE_RETRY_MAX", "300")))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = str(args.token or "").strip()
    if len(token) < 32:
        raise SystemExit("--token or EVE_SENTRY_ESI_GATEWAY_TOKEN must be at least 32 characters")
    allowed = set(args.allowed_client or os.environ.get("EVE_SENTRY_ESI_GATEWAY_ALLOWED_CLIENTS", "").replace(",", " ").split())
    if not 5 <= args.refresh_interval <= 10:
        raise SystemExit("--refresh-interval must be between 5 and 10 seconds")
    if not 1 <= args.refresh_batch_size <= 1000:
        raise SystemExit("--refresh-batch-size must be between 1 and 1000")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    id_cache = None
    if args.postgres_dsn or args.redis_url:
        durable = PostgresStore(args.postgres_dsn) if args.postgres_dsn else MemoryStore()
        hot = RedisHotStore(args.redis_url) if args.redis_url else None
        id_cache = IdCacheCoordinator(
            durable,
            hot,
            ttl_seconds=args.id_cache_ttl,
            ttl_by_endpoint={
                "resolve_names": args.id_cache_ttl,
                "resolve_ids": args.id_cache_ttl,
                "get_character": args.character_cache_ttl,
                "get_character_affiliations": args.affiliation_cache_ttl,
                "get_corporation": args.corporation_cache_ttl,
                "get_alliance": args.alliance_cache_ttl,
                "get_system": args.system_cache_ttl,
            },
            stale_grace_seconds=args.stale_grace,
            refresh_interval_seconds=args.refresh_interval,
            refresh_batch_size=args.refresh_batch_size,
            retry_base_seconds=args.cache_retry_base,
            retry_max_seconds=args.cache_retry_max,
        )
    state = GatewayState(token, allowed, args.cache_ttl, args.rate, max_cache_entries=max(1, args.cache_max_entries), negative_ttl=args.negative_ttl, stale_grace=args.stale_grace, id_cache=id_cache)
    server = GatewayServer((args.host, args.port), state)
    logging.getLogger("esi_gateway").info("listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
