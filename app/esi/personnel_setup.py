"""Opt-in startup and isolated private contact snapshots for the personnel archive."""

from __future__ import annotations

import os
import threading
import time

from app.intel.enrichment import ThreatEnricher
from app.esi.personnel_archive import PersonnelArchive
from app.esi.personnel_runtime import PersonnelResolver, PersonnelRuntime


class PersonnelEnricher(ThreatEnricher):
    """Only the refresh lane accesses private ESI; hot reads return a bounded-age copy."""

    def __init__(self, resolver, session, *, now=time.time):
        super().__init__(resolver=resolver, esi_session=session, now=now)
        self._contacts_lock = threading.Lock()
        self._context = None
        self._successful_at = 0.0

    def contact_standings(self):
        with self._contacts_lock:
            if self._now() - self._successful_at > 3600:
                return []
            return list(self._contact_standings or [])

    def refresh_contacts(self) -> bool:
        from app.intel.enrichment import _normalize_contact_standings
        session = self.esi_session
        if session is None:
            return False
        try:
            tokens = session.load_tokens(refresh_if_needed=False)
            context = (tokens.character_id, tokens.character_owner_hash, tuple(sorted(tokens.scopes)))
        except Exception:
            context = None
        with self._contacts_lock:
            changed = context != self._context
            if changed or context is None:
                self._context = context
                self._contact_standings = []
                self._successful_at = 0
                self._contact_standings_until = 0
            if context is None or self._now() < self._contact_standings_until:
                return changed
        try:
            snapshot = session.snapshot(include_location=False, include_contacts=True)
            current = snapshot.tokens
            if (current.character_id, current.character_owner_hash, tuple(sorted(current.scopes))) != context:
                return True
            contacts = _normalize_contact_standings(snapshot.contacts)
        except Exception:
            with self._contacts_lock:
                self._contact_standings_until = self._now() + 30
            return changed
        with self._contacts_lock:
            changed = changed or contacts != self._contact_standings
            self._contact_standings = contacts
            self._successful_at = self._now()
            self._contact_standings_until = self._now() + 300
        return changed


def configure_personnel(store, args, resolver, *, environment=None):
    """Off is a zero-I/O rollback; shadow writes archives without changing consumers."""
    environment = os.environ if environment is None else environment
    mode = environment.get("EVE_SENTRY_PERSONNEL_CACHE", "off").strip().lower()
    if mode not in {"off", "shadow", "on"}:
        raise ValueError("EVE_SENTRY_PERSONNEL_CACHE must be off, shadow, or on")
    if mode == "off":
        return
    if args.storage != "postgres" or resolver is None:
        raise ValueError("personnel cache requires PostgreSQL and enabled ESI")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from psycopg.conninfo import make_conninfo, conninfo_to_dict

    options = conninfo_to_dict(args.postgres_dsn).get("options", "")
    dsn = make_conninfo(args.postgres_dsn, options=options + " -cstatement_timeout=5000 -clock_timeout=2000")
    pool = ConnectionPool(dsn, min_size=1, max_size=8, timeout=2, open=True, kwargs={"row_factory": dict_row})
    try:
        archive = PersonnelArchive(pool.connection)
        archive.migrate()
        runtime = PersonnelRuntime(archive, resolver.client)
        runtime.mode = mode
        from app.esi.personnel_backfill import PersonnelBackfill
        runtime.backfill = PersonnelBackfill(archive, resolver.cache)
        store._personnel_pool = pool
        store._personnel_runtime = runtime
        if mode == "on":
            replacement = PersonnelResolver(resolver, runtime)
            session = getattr(store._enricher, "esi_session", None)
            enricher = PersonnelEnricher(replacement, session)
            store._resolver = replacement
            store._enricher = enricher
            runtime.contact_refresher = enricher.refresh_contacts
        runtime.start(store.refresh_personnel)
    except Exception:
        pool.close()
        raise
