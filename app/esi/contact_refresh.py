"""Shared contact refresh fencing and bounded failure retention for both cache modes."""

import logging
import math
import random

from app.esi.contact_http import failure_details

LOG = logging.getLogger(__name__)


def token_context(tokens):
    return tokens.character_id, tokens.character_owner_hash, tuple(sorted(tokens.scopes))


def current_context(session):
    loader = getattr(session, "load_tokens", None)
    if loader is None:
        # Compatibility with in-process adapters; never used by the production session.
        return (id(session),)
    return token_context(loader(refresh_if_needed=False))


def failure_context(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if hasattr(exc, "contacts_context"):
            return exc.contacts_context
        exc = exc.__cause__
    return None


def cached_contacts(owner):
    with owner._contacts_lock:
        if owner._successful_at is None or owner._now() - owner._successful_at >= 3600:
            return []
        return list(owner._contact_standings or [])


def refresh_contacts(owner):
    """No state lock held during I/O; one writer, and authority rechecked before publish."""
    from app.intel.enrichment import _normalize_contact_standings

    session = owner.esi_session
    if session is None or not hasattr(session, "snapshot"):
        return False
    if not owner._contacts_refresh_lock.acquire(blocking=False):
        return False
    try:
        try:
            context = current_context(session)
        except Exception:  # noqa: BLE001 -- Any credential-store failure isolates private data.
            context = None
        with owner._contacts_lock:
            changed = context != owner._context
            if changed or context is None:
                owner._context = context
                owner._contact_standings = []
                owner._successful_at = None
                owner._contact_standings_until = 0.0
                owner._contacts_org_context = None
                owner._contacts_failures = 0
            expired = owner._successful_at is not None and owner._now() - owner._successful_at >= 3600
            if expired:
                changed = changed or bool(owner._contact_standings)
                owner._contact_standings = []
                owner._successful_at = None
            if context is None or owner._now() < owner._contact_standings_until:
                return changed
        try:
            contacts_loader = getattr(session, "contacts_snapshot", None)
            snapshot = (contacts_loader() if contacts_loader is not None else
                        session.snapshot(include_location=False, include_contacts=True))
            contacts = _normalize_contact_standings(snapshot.contacts)
            # A replaced/revoked login must fence even an already completed request.
            latest = current_context(session)
            result_tokens = getattr(snapshot, "tokens", None)
            result_context = token_context(result_tokens) if result_tokens is not None else context
            if latest != context or result_context != context:
                with owner._contacts_lock:
                    owner._context = None
                    owner._contact_standings = []
                    owner._successful_at = None
                    owner._contact_standings_until = 0.0
                return True
            expires = getattr(snapshot, "contacts_expires_at", None)
            # Legacy adapters retain their configured TTL; real session always supplies metadata.
            expires = owner._now() + owner.standing_ttl_seconds if expires is None else float(expires)
            if not math.isfinite(expires):
                raise ValueError("invalid_contacts_expiry")
        except Exception as exc:  # noqa: BLE001 -- Refresh boundary retains data and logs safe error types.
            now = owner._now()
            status, retry_after = failure_details(exc, now)
            org_context = failure_context(exc)
            try:
                authority_changed = current_context(session) != context
            except Exception:  # noqa: BLE001 -- Cannot establish authority: fail closed.
                authority_changed = True
            with owner._contacts_lock:
                owner._contacts_failures += 1
                owner._contacts_status = "unavailable" if status in {401, 403} else "degraded"
                if (authority_changed or status in {401, 403}
                        or (org_context is not None and org_context != owner._contacts_org_context)):
                    changed = changed or bool(owner._contact_standings)
                    owner._contact_standings = []
                    owner._successful_at = None
                if authority_changed:
                    owner._context = None
                delay = min(300.0, 30.0 * 2 ** min(owner._contacts_failures - 1, 4))
                delay *= 1.0 + random.random() * 0.1
                if status in {401, 403, 420, 429}:
                    delay = max(delay, 300.0)
                owner._contact_standings_until = now + max(delay, retry_after)
            LOG.warning("contacts_refresh status=%s error=%s failures=%s retry_after=%.1f",
                        status, type(exc).__name__, owner._contacts_failures, max(delay, retry_after))
            return changed
        with owner._contacts_lock:
            org_context = getattr(snapshot, "contacts_context", None)
            changed = changed or contacts != owner._contact_standings or org_context != owner._contacts_org_context
            owner._contact_standings = contacts
            owner._contacts_org_context = org_context
            owner._successful_at = owner._now()
            owner._contacts_failures = 0
            owner._contacts_status = "degraded" if getattr(snapshot, "contacts_degraded", False) else "fresh"
            # A zero remaining TTL is already expired, but must not create a hot retry loop.
            owner._contact_standings_until = max(owner._now() + 5, min(expires, owner._now() + 300))
        return changed
    finally:
        owner._contacts_refresh_lock.release()
