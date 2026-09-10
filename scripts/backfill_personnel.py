"""Opt-in personnel archive backfill. DSN is read from an environment variable."""

from __future__ import annotations

import argparse
import os


def main():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from app.esi.cache import EsiCache
    from app.esi.personnel_archive import PersonnelArchive
    from app.esi.personnel_backfill import PersonnelBackfill

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="EVE_SENTRY_POSTGRES_DSN")
    parser.add_argument("--legacy-cache")
    parser.add_argument("--max-batches", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.max_batches <= 10000:
        parser.error("max-batches must be 1-10000")
    with ConnectionPool(os.environ[args.dsn_env], min_size=1, max_size=1, timeout=5,
                        kwargs={"row_factory": dict_row}) as pool:
        archive = PersonnelArchive(pool.connection)
        archive.migrate()
        backfill = PersonnelBackfill(archive, EsiCache(args.legacy_cache) if args.legacy_cache else None)
        for source in (("legacy", "history") if args.legacy_cache else ("history",)):
            for _ in range(args.max_batches):
                if not backfill.step(source):
                    break
        print("Backfill batch complete; durable cursors retained. No ESI requests performed.")


if __name__ == "__main__":
    main()
