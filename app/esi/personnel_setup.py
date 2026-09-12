"""Opt-in startup and isolated private contact snapshots for the personnel archive."""

from __future__ import annotations

import os
import time

from app.intel.enrichment import ThreatEnricher


class PersonnelEnricher(ThreatEnricher):
    """Only the refresh lane accesses private ESI; hot reads return a bounded-age copy."""

    def __init__(self, resolver, session, *, now=time.time, relations=None):
        super().__init__(resolver=resolver, esi_session=session, now=now)
        self.relations = relations
        from app.esi.relation_shadow import ShadowComparisons
        self.shadow_comparisons = ShadowComparisons()

    def contact_standings(self):
        mode = getattr(self, "organization_mode", "on")
        if self.relations is not None and mode == "on":
            return self.relations.view()
        from app.esi.contact_refresh import cached_contacts
        contacts = cached_contacts(self)
        if self.relations is not None and mode == "shadow":
            from app.esi.relation_shadow import ShadowContacts
            return ShadowContacts(contacts, self.relations.view(), self.shadow_comparisons)
        return contacts

    def refresh_contacts(self) -> bool:
        mode = getattr(self, "organization_mode", "on")
        if self.relations is not None and mode == "on":
            return self.relations.refresh()
        from app.esi.contact_refresh import refresh_contacts
        changed = refresh_contacts(self)
        if self.relations is not None and mode == "shadow":
            self.relations.refresh()
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
