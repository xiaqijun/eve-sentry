"""Opt-in startup and isolated private contact snapshots for the personnel archive."""

from __future__ import annotations

import os
import threading
import time

from app.intel.enrichment import ThreatEnricher


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


def configure_personnel(store, args, resolver, *, environment=None, configuration=None):
    """Off is a zero-I/O rollback; shadow writes archives without changing consumers."""
    environment = os.environ if environment is None else environment
    from app.server.personnel_settings import validate_settings

    configuration = validate_settings(configuration) if configuration is not None else None
    mode = configuration["mode"] if configuration is not None else environment.get("EVE_SENTRY_PERSONNEL_CACHE", "off").strip().lower()
    if mode not in {"off", "shadow", "on"}:
        raise ValueError("EVE_SENTRY_PERSONNEL_CACHE must be off, shadow, or on")
    if mode == "off":
        return
    if args.storage != "postgres" or resolver is None:
        raise ValueError("personnel cache requires PostgreSQL and enabled ESI")
    from app.esi.personnel_control import personnel_controller
    from app.server.personnel_settings import DEFAULTS

    controller = personnel_controller(store, args, resolver)
    values = configuration or {**DEFAULTS, "mode": mode}
    with controller.lock:
        prepared = controller.prepare(values)
        controller.publish(values, prepared)
