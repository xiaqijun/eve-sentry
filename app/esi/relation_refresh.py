"""Background-only organization relationship refresh with independent source snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import replace

from app.esi.contact_http import ContactReadError, failure_details
from app.esi.contact_refresh import token_context
from app.esi.organization_relations import (
    RULE_VERSION,
    RelationSource,
    RelationView,
    organization_entries,
)
from app.esi.personnel_policy import positive_id, timestamp

LOG = logging.getLogger(__name__)


class OrganizationRelations:
    def __init__(self, session, repository=None, *, now=time.time):
        self.session, self.repository, self.now = session, repository, now
        self._writer = threading.Lock()
        self._lock = threading.Lock()
        self._auth = None
        self._context = ""
        self._corporation = self._alliance = self._own_success = None
        self._own_due = 0.0
        self._own_failures = 0
        self._sources = ()
        self._view = None
        self._view_key = None

    def view(self):
        """Read/fence expiry in memory only; reuse immutable dictionaries across people."""
        now = self.now()
        with self._lock:
            own_usable = self._own_success is not None and self._own_success <= now < self._own_success + 3600
            usable = tuple(source.usable(now) for source in self._sources)
            key = self._context, own_usable, tuple((source.revision, ok) for source, ok in zip(self._sources, usable))
            if key != self._view_key:
                self._view_key = key
                self._view = RelationView(self._context, self._corporation, self._alliance,
                                          own_usable, self._sources, usable)
            return self._view

    def _reset(self, auth):
        with self._lock:
            self._auth, self._context, self._sources = auth, "", ()
            self._corporation = self._alliance = self._own_success = None
            self._own_due, self._own_failures = 0.0, 0
            self._view_key = None

    def _same_authority(self, auth):
        try:
            return token_context(self.session.load_tokens(refresh_if_needed=False)) == auth
        except Exception:  # noqa: BLE001 -- Inability to establish authority must isolate private data.
            return False

    def _own_profile(self, tokens):
        profile = self.session.esi_client.get_character(positive_id(tokens.character_id))
        if not isinstance(profile, dict):
            raise ContactReadError("invalid_own_organization")
        corporation = positive_id(profile.get("corporation_id"))
        alliance = profile.get("alliance_id")
        if alliance is not None:
            alliance = positive_id(alliance)
        return corporation, alliance

    def _activate_context(self, auth, corporation, alliance):
        context = hashlib.sha256(json.dumps((RULE_VERSION, auth, corporation, alliance),
                                            sort_keys=True).encode()).hexdigest()
        if context != self._context or not self._sources:
            # Isolate the old organization before even attempting a new database read.
            with self._lock:
                self._context, self._sources = context, ()
                self._corporation, self._alliance = corporation, alliance
                self._own_success = None
                self._view_key = None
            sources = []
            for kind, entity_id in (("corporation", corporation), ("alliance", alliance)):
                if entity_id is None:
                    continue
                source = (self.repository.load(context, kind, entity_id) if self.repository else
                          RelationSource(kind, entity_id))
                # Persisted permission may be older than a failed revocation
                # write. Require a successful read in this runtime before use;
                # retain failure Retry-After across restart, never bypass it.
                source = replace(source, authorized=False,
                                 retry_at=source.retry_at if source.failures else 0.0)
                sources.append(source)
            with self._lock:
                self._sources = tuple(sources)
        with self._lock:
            self._own_success = self.now()
            self._own_due = self.now() + 300
            self._own_failures = 0

    def _save(self, before, source):
        if self.repository:
            saved = self.repository.save(self._context, source, expected_revision=before.revision)
            return saved or self.repository.load(self._context, source.kind, source.entity_id)
        return replace(source, revision=before.revision + 1)

    def _refresh_source(self, source, tokens):
        now = self.now()
        scope = f"esi-{source.kind}s.read_contacts.v1"
        if now < source.retry_at:
            return source
        if scope not in tokens.scopes:
            return self._save(source, replace(source, authorized=False, retry_at=now + 300))
        try:
            rows = getattr(self.session.esi_client, f"get_{source.kind}_contacts")(
                source.entity_id, tokens.access_token)
            entries = organization_entries(rows)
            expires = timestamp(getattr(rows, "expires_at", 0.0))
            if expires <= self.now() or getattr(rows, "degraded", False):
                raise ContactReadError("organization_contacts_expiry_unavailable")
            result = replace(source, successful_at=self.now(), expires_at=expires,
                             retry_at=max(self.now() + 5, min(expires, self.now() + 300)),
                             authorized=True, failures=0, entries=entries)
        except Exception as exc:  # noqa: BLE001 -- One source failure must not erase the other source.
            status, retry_after = failure_details(exc, self.now())
            failures = source.failures + 1
            delay = min(300, 30 * 2 ** min(failures - 1, 4)) * (1 + random.random() * .1)
            if status in {401, 403, 420, 429}:
                delay = max(delay, 300)
            result = replace(source, failures=failures,
                             authorized=source.authorized and status not in {401, 403},
                             retry_at=self.now() + max(delay, retry_after))
            LOG.warning("organization_contacts source=%s status=%s error=%s retry_after=%.1f",
                        source.kind, status, type(exc).__name__, max(delay, retry_after))
        if not self._same_authority(self._auth):
            raise ContactReadError("organization_authority_changed")
        try:
            return self._save(source, result)
        except Exception as exc:  # noqa: BLE001 -- Never preserve a revoked relation because its DB write failed.
            LOG.warning("organization_snapshot_persist error=%s", type(exc).__name__)
            return replace(source, authorized=source.authorized and result.authorized,
                           retry_at=self.now() + 30)

    def refresh(self):
        if not self._writer.acquire(blocking=False):
            return False
        before = self.view()
        try:
            try:
                tokens = self.session.load_tokens(refresh_if_needed=False)
                auth = token_context(tokens)
            except Exception:  # noqa: BLE001 -- Missing/revoked token store is not a stale authority.
                self._reset(None)
                return before != self.view()
            if auth != self._auth:
                self._reset(auth)
            if self.now() >= self._own_due:
                try:
                    tokens = self.session.load_tokens()
                    if token_context(tokens) != auth:
                        self._reset(None)
                        return before != self.view()
                    corporation, alliance = self._own_profile(tokens)
                    if not self._same_authority(auth):
                        self._reset(None)
                        return before != self.view()
                    self._activate_context(auth, corporation, alliance)
                except Exception as exc:  # noqa: BLE001 -- Preserve only bounded, same-context data.
                    status, retry = failure_details(exc, self.now())
                    if status in {401, 403}:
                        self._reset(auth)
                    elif not self._same_authority(auth):
                        self._reset(None)
                    self._own_failures += 1
                    self._own_due = self.now() + max(retry, 300 if status in {401, 403, 420, 429} else
                                                     min(300, 30 * 2 ** min(self._own_failures - 1, 4)))
                    LOG.warning("organization_context status=%s error=%s", status, type(exc).__name__)
                    return before != self.view()
            if not self._context or self._own_success is None:
                return before != self.view()
            # Refresh only sources which are due, while preserving separate success/failure state.
            for index, source in enumerate(self._sources):
                try:
                    if self.now() >= source.retry_at:
                        tokens = self.session.load_tokens()
                        if token_context(tokens) != auth:
                            self._reset(None)
                            break
                    updated = self._refresh_source(source, tokens)
                    if not self._same_authority(auth):
                        self._reset(None)
                        break
                    with self._lock:
                        values = list(self._sources)
                        values[index] = updated
                        self._sources = tuple(values)
                except Exception as exc:  # noqa: BLE001 -- Storage failures keep prior committed state.
                    if not self._same_authority(auth):
                        self._reset(None)
                        break
                    with self._lock:
                        values = list(self._sources)
                        values[index] = replace(source, retry_at=self.now() + 30)
                        self._sources = tuple(values)
                    LOG.warning("organization_snapshot error=%s", type(exc).__name__)
            return before != self.view()
        finally:
            self._writer.release()
