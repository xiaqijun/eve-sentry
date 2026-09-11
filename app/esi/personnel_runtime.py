"""Bounded archive-backed identity service, with separate realtime/background lanes."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import OrderedDict, Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from typing import Any

from app.esi.personnel_archive import (
    PersonnelArchive, IdentityUpdate, AffiliationUpdate, OrganizationUpdate,
)
from app.esi.personnel_policy import (
    AFFILIATION_TTL, NAME_REFRESH_SECONDS, name_key, personnel_tier, refresh_due_at, background_slots, RefreshLoad,
)
from app.esi.resolver import EsiResolver, ResolvedName

logger = logging.getLogger(__name__)


class PersonnelRuntime:
    """No SQL or network on hot reads; all persistence happens off the store lock."""

    def __init__(self, archive: PersonnelArchive, client: Any, *, max_hot: int = 20000, now=time.time):
        self.archive, self.client, self.now = archive, client, now
        self.mode = "on"
        self.max_hot = max_hot
        self._lock = threading.RLock()
        self._profiles: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._names: dict[str, int] = {}
        self._pending: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._pending_ids: set[int] = set()
        self._negative: OrderedDict[str, float] = OrderedDict()
        self._sightings: OrderedDict[str, float] = OrderedDict()
        self._upstream_until: dict[int, float] = {}
        self._active: set[int] = set()
        self._active_names: set[str] = set()
        self._changed: set[int] = set()
        self._stats: dict[str, Any] = {}
        self._counts: Counter[str] = Counter()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._callback = None
        self.contact_refresher = None
        self.backfill = None
        self._legacy_import_done = False
        self._cursor = 0
        self._background = 1
        self._retry_until = 0.0
        self._latency_ms = 0.0
        self._db_wait_ms = 0.0
        self._last_request = 0.0
        self._budget = threading.Lock()
        self._scheduling = {"background_refresh": True, "history_backfill": True, "background_max": 4}

    def scheduling_settings(self):
        with self._lock:
            return dict(self._scheduling)

    def configure_scheduling(self, *, background_refresh, history_backfill, background_max):
        if type(background_refresh) is not bool or type(history_backfill) is not bool:
            raise ValueError("scheduler switches must be booleans")
        if type(background_max) is not int or not 1 <= background_max <= 4:
            raise ValueError("background_max must be 1-4")
        with self._lock:
            next_settings = {"background_refresh": background_refresh, "history_backfill": history_backfill,
                             "background_max": background_max}
            if next_settings == self._scheduling:
                return
            self._scheduling = next_settings
            self._background = min(self._background, background_max) if background_refresh else 0
        self._wake.set()

    def lookup(self, name: str) -> dict[str, Any] | None:
        key = name_key(name)
        with self._lock:
            row = self._profiles.get(self._names.get(key))
            self._counts["hot_hits" if row else "hot_misses"] += 1
            return dict(row) if row else None

    def profile(self, character_id: int) -> dict[str, Any] | None:
        with self._lock:
            value = self._profiles.get(character_id)
            if not value:
                if len(self._pending_ids) < self.max_hot and character_id > 0:
                    self._pending_ids.add(character_id)
                    self._wake.set()
                return None
            result = dict(value)
        fetched = float(result.get("affiliation_fetched_at") or 0)
        result.update(
            cache_status="cached" if fetched > 0 and self.now() - fetched < AFFILIATION_TTL else "stale",
            fetched_at=fetched, affiliation_trusted=fetched > 0 and self.now() - fetched < AFFILIATION_TTL,
            zkill_url=f"https://zkillboard.com/character/{character_id}/",
        )
        return result

    def request(self, name: str, *, seen_at: float | None = None) -> None:
        key = name_key(name)
        with self._lock:
            if self._negative.get(key, 0) > self.now():
                return
            if key not in self._pending and len(self._pending) >= self.max_hot:
                self._counts["queue_rejected"] += 1
                return
            seen = self.now() if seen_at is None else seen_at
            seen = max(seen, self._sightings.get(key, 0))
            self._pending[key] = (name.strip(), seen)
            self._sightings[key] = self._pending[key][1]
            self._sightings.move_to_end(key)
            while len(self._sightings) > self.max_hot:
                self._sightings.popitem(last=False)
        self._wake.set()

    def set_active(self, identities: list[tuple[int | None, str, float]]) -> None:
        with self._lock:
            self._active = {row[0] for row in identities if row[0]}
            self._active_names = {name_key(row[1]) for row in identities}
        for _, name, seen_at in identities:
            self.request(name, seen_at=seen_at)

    def _remember(self, rows: list[dict[str, Any]]) -> None:
        # Organization names are shared in SQL; only assemble bounded hot profiles.
        organizations = {}
        for kind in ("corporation", "alliance"):
            ids = list({row[kind + "_id"] for row in rows if row.get(kind + "_id")})
            organizations[kind] = self.archive.get_organizations(kind, ids) if ids else {}
        with self._lock:
            for source in rows:
                row = dict(source)
                for kind, values in organizations.items():
                    org = values.get(row.get(kind + "_id"))
                    if org:
                        row[kind + "_name"] = org["name"]
                cid = row["character_id"]
                previous = self._profiles.get(cid)
                if previous and previous["revision"] > row["revision"]:
                    continue
                if previous:
                    self._names.pop(name_key(previous["name"]), None)
                if previous is None or any(previous.get(key) != row.get(key) for key in
                                           ("name", "revision", "affiliation_fetched_at", "corporation_name", "alliance_name")):
                    self._changed.add(cid)
                self._profiles[cid] = row
                self._profiles.move_to_end(cid)
                self._names[name_key(row["name"])] = cid
            while len(self._profiles) > self.max_hot:
                cid, old = self._profiles.popitem(last=False)
                self._names.pop(name_key(old["name"]), None)
                self._upstream_until.pop(cid, None)

    def _schedule(self, row: dict[str, Any], *, seen_at: float | None = None) -> None:
        now = self.now()
        cid = row["character_id"]
        with self._lock:
            active = cid in self._active or name_key(row["name"]) in self._active_names
        tier = personnel_tier(now=now, last_seen_at=seen_at if seen_at is not None else row["last_seen_at"], active=active)
        fetched = float(row.get("affiliation_fetched_at") or 0)
        due = refresh_due_at(character_id=cid, fetched_at=fetched, tier=tier,
                             upstream_valid_until=self._upstream_until.get(cid, 0),
                             spare_capacity=self._background > 1)
        self.archive.request_refresh("affiliation", cid, priority=tier, due_at=now if not fetched else due, promote_only=True)
        # Repair legacy multi-day schedules for every tier, including cold history.
        # Keep retries bounded and never steal an in-flight lease.
        self.archive.expedite_stale_affiliation(cid, now=now, due_at=due if fetched else now)
        self.archive.request_refresh("identity", cid, priority=tier,
                                     due_at=float(row["name_checked_at"]) + NAME_REFRESH_SECONDS, promote_only=True)
        for kind in ("corporation", "alliance"):
            entity_id = row.get(kind + "_id")
            if entity_id and not self.archive.get_organizations(kind, [entity_id]):
                self.archive.request_refresh(kind, entity_id, priority=2, due_at=now)

    def drain_requests(self) -> None:
        with self._lock:
            batch = [self._pending.popitem(last=False)[1] for _ in range(min(500, len(self._pending)))]
            ids = list(self._pending_ids)[:500]
            self._pending_ids.difference_update(ids)
        started = time.monotonic()
        try:
            if ids:
                profiles = self.archive.get_profiles(ids)
                self._remember(list(profiles.values()))
                for cid in ids:
                    if cid not in profiles:
                        self.archive.request_refresh("identity", cid, priority=0, due_at=self.now())
                    else:
                        self._schedule(profiles[cid])
            if not batch:
                return
            found = self.archive.find_names([row[0] for row in batch])
            self._counts["database_hits"] += len(found)
            self._counts["database_misses"] += len(batch) - len(found)
            self._remember(list(found.values()))
            for name, seen in batch:
                row = found.get(name_key(name))
                if row:
                    self.archive.observe([row["character_id"]], seen)
                    self._schedule(row, seen_at=seen)
                else:
                    self.archive.request_refresh("resolve", name, priority=0, due_at=self.now())
        except Exception:
            with self._lock:
                self._pending_ids.update(ids[:max(0, self.max_hot - len(self._pending_ids))])
            for name, seen in batch:
                self.request(name, seen_at=seen)
            raise
        finally:
            self._db_wait_ms = (time.monotonic() - started) * 1000 / max(1, len(batch))

    def _freshness(self, key: str) -> tuple[float, float]:
        method = getattr(self.client, "response_freshness", None)
        if not callable(method):
            return self.now(), 0.0
        metadata = method()
        item = metadata.get(key, metadata.get("_direct", {}))
        fetched = float(item.get("fetched_at") or 0)
        expires = float(item.get("expires_at") or 0)
        if not 0 <= fetched <= self.now() + 60 or not 0 <= expires < float("inf"):
            raise ValueError("invalid upstream freshness")
        return fetched, expires

    def run_batch(self, kind: str, *, realtime: bool, cold: bool = False) -> int:
        if not realtime and not self.scheduling_settings()["background_refresh"]:
            return 0
        now = self.now()
        leases = self.archive.claim(now=now, limit=100 if kind in {"resolve", "identity", "affiliation"} else 1,
                                    lease_seconds=90, minimum_priority=0 if realtime else 2,
                                    maximum_priority=1 if realtime else 5, oldest_first=cold, kind=kind)
        if not leases:
            return 0
        started = time.monotonic()
        try:
            # Shared request pacing; background workers also honor global backoff.
            with self._budget:
                wait = max(0, self._last_request + 0.2 - time.monotonic(), self._retry_until - self.now())
                if self._stop.wait(min(wait, 60)) or wait > 60:
                    raise TimeoutError("refresh stopped or throttled")
                self._last_request = time.monotonic()
            keys = [lease.entity_key for lease in leases]
            if kind == "resolve":
                payload = self.client.resolve_ids(keys)
                rows = {name_key(row["name"]): row for row in payload.get("characters", [])}
            elif kind == "identity":
                rows = {str(row["id"]): row for row in self.client.resolve_names([int(key) for key in keys])
                        if row.get("category") == "character"}
            elif kind == "affiliation":
                rows = {str(row["character_id"]): row for row in self.client.get_character_affiliations([int(key) for key in keys])}
            else:
                rows = {keys[0]: getattr(self.client, "get_" + kind)(int(keys[0]))}
            # Capture metadata before any subsequent public request on this thread.
            freshness = {key: self._freshness(key) for key in keys}
            for lease in leases:
                row = rows.get(lease.entity_key)
                if not row:
                    # A partial affiliation response is never a null affiliation.
                    self.archive.fail(lease, now=self.now(), error_code="not_found", retry_after=60)
                    if kind == "resolve":
                        with self._lock:
                            self._negative[lease.entity_key] = self.now() + 60
                            while len(self._negative) > self.max_hot:
                                self._negative.popitem(last=False)
                    continue
                fetched, expires = freshness[lease.entity_key]
                cid = None
                if kind in {"resolve", "identity"}:
                    cid = int(row["id"])
                    existing = self.archive.get_profiles([cid]).get(cid)
                    with self._lock:
                        seen = max(existing["last_seen_at"] if existing else 0.0,
                                   self._sightings.get(name_key(row["name"]), 0.0))
                    update = IdentityUpdate(cid, row["name"], fetched, seen)
                    due = max(self.now() + 60, fetched + NAME_REFRESH_SECONDS, expires)
                    if kind == "resolve":
                        due = self.now() + 36500 * 86400  # Identity-by-ID owns subsequent name validation.
                elif kind == "affiliation":
                    cid = int(lease.entity_key)
                    update = AffiliationUpdate(cid, int(row["corporation_id"]), fetched,
                                               row.get("alliance_id"), row.get("faction_id"))
                    profile = self.archive.get_profiles([cid])[cid]
                    with self._lock:
                        active = cid in self._active or name_key(profile["name"]) in self._active_names
                    tier = personnel_tier(now=self.now(), last_seen_at=profile["last_seen_at"], active=active)
                    due = max(self.now() + 60, refresh_due_at(character_id=cid, fetched_at=fetched,
                              tier=tier, upstream_valid_until=expires, spare_capacity=self._background > 1))
                    if not fetched or self.now() - fetched >= AFFILIATION_TTL:
                        # Still-old responses during rolling deployment must not
                        # defer confirmation another day or create a tight loop.
                        due = self.now() + 60
                else:
                    update = OrganizationUpdate(kind, int(lease.entity_key), row["name"], fetched)
                    due = self.now() + 36500 * 86400  # Names have no periodic refresh obligation.
                if self.archive.finish(lease, now=self.now(), next_due_at=due,
                                       next_priority=tier if kind == "affiliation" else 2, update=update):
                    self._counts["refresh_success"] += 1
                    if cid:
                        if kind == "affiliation":
                            with self._lock:
                                self._upstream_until[cid] = expires
                        profiles = list(self.archive.get_profiles([cid]).values())
                        self._remember(profiles)
                        if kind in {"resolve", "identity"}:
                            for profile in profiles:
                                self._schedule(profile)
                    else:
                        with self._lock:
                            affected = [cid for cid, p in self._profiles.items() if p.get(kind + "_id") == int(lease.entity_key)]
                        for start in range(0, len(affected), 500):
                            self._remember(list(self.archive.get_profiles(affected[start:start + 500]).values()))
                else:
                    self._counts["late_results"] += 1
            self._latency_ms = (time.monotonic() - started) * 1000
        except Exception as exc:
            self._counts["refresh_errors"] += 1
            delay = 5.0
            cause = exc
            while cause is not None:
                headers = getattr(cause, "headers", None)
                if headers:
                    try:
                        raw = headers.get("Retry-After", "5")
                        try:
                            retry = float(raw)
                        except ValueError:
                            retry = parsedate_to_datetime(raw).timestamp() - self.now()
                        if math.isfinite(retry):
                            delay = max(delay, retry)
                    except (ValueError, TypeError):
                        pass
                cause = cause.__cause__
            self._retry_until = max(self._retry_until, self.now() + delay)
            for lease in leases:
                try:
                    self.archive.fail(lease, now=self.now(), error_code="throttled" if delay > 5 else "upstream", retry_after=delay)
                except Exception:
                    self._counts["storage_errors"] += 1
            logger.warning("Personnel refresh batch failed (%s); successful cache retained", kind)
        return len(leases)

    def maintenance(self) -> None:
        rows = self.archive.page(self._cursor, 100)
        if not rows:
            self._cursor = 0
            return
        for row in rows:
            self._schedule(row)
        self._cursor = rows[-1]["character_id"]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._stats, **self._counts, "mode": self.mode, "hot_profiles": len(self._profiles),
                    "scheduling": dict(self._scheduling),
                    "pending_names": len(self._pending), "background_slots": self._background,
                    "upstream_batch_ms": self._latency_ms, "database_item_ms": self._db_wait_ms,
                    "degraded": self._retry_until > self.now()}

    def start(self, callback, *, paused: bool = False) -> None:
        self._callback = callback
        self._ready = threading.Event()
        if not paused:
            self._ready.set()
        self._thread = threading.Thread(target=self._run, name="personnel-refresh", daemon=True)
        self._thread.start()

    def activate(self) -> None:
        with self._lock:
            self._force_current = True
        self._ready.set()
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()
        ready = getattr(self, "_ready", None)
        if ready is not None:
            ready.set()

    def close(self, *, timeout: float | None = 60) -> None:
        self.request_stop()
        if self._thread and self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        ready = getattr(self, "_ready", None)
        if ready is not None:
            ready.wait()
        kinds = ("resolve", "affiliation", "identity", "corporation", "alliance")
        futures = {}
        cycle = 0
        force_current = False
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="personnel") as executor:
            while not self._stop.is_set():
                try:
                    for future in list(futures):
                        if future.done():
                            lane = futures.pop(future)
                            outcome = future.result()
                            if lane[1] == "contacts" and outcome:
                                force_current = True
                    self.drain_requests()
                    with self._lock:
                        changed, self._changed = self._changed, set()
                        force_current = force_current or getattr(self, "_force_current", False)
                        self._force_current = False
                    if self._callback and (changed or force_current or cycle % 5 == 0):
                        try:
                            accepted = self._callback(changed, force_current or cycle % 30 == 0)
                        except Exception:
                            with self._lock:
                                self._changed.update(changed)
                            raise
                        force_current = accepted is False
                    self._stats = self.archive.statistics(self.now())
                    controls = self.scheduling_settings()
                    if cycle % 5 == 0:
                        realtime_due = any(row["priority"] <= 1 for row in self._stats["due_by_priority"])
                        self._background = background_slots(self._background, RefreshLoad(
                            cpu_fraction=os.getloadavg()[0] / max(1, os.cpu_count() or 1) if hasattr(os, "getloadavg") else 0,
                            realtime_pending=int(realtime_due), database_wait_ms=self._db_wait_ms,
                            upstream_latency_ms=self._latency_ms, throttled=self._retry_until > self.now()),
                            maximum=controls["background_max"]) if controls["background_refresh"] else 0
                        if controls["background_refresh"]:
                            self.maintenance()
                        if self.backfill and controls["history_backfill"] and not realtime_due:
                            if not self._legacy_import_done:
                                self._legacy_import_done = not self.backfill.step("legacy")
                            else:
                                self.backfill.step("history")
                    if (self.contact_refresher and cycle % 5 == 0
                            and (False, "contacts") not in futures.values()
                            and sum(not lane[0] for lane in futures.values()) < 4):
                        future = executor.submit(self.contact_refresher)
                        futures[future] = (False, "contacts")
                    capacity = min(self._background, controls["background_max"]) if controls["background_refresh"] else 0
                    for realtime, capacity in ((True, 2), (False, capacity)):
                        if self._retry_until > self.now():
                            continue
                        running = [value for value in futures.values() if value[0] == realtime]
                        due = {row["kind"] for row in self._stats["due_by_priority"]
                               if (row["priority"] <= 1) == realtime}
                        ordered = kinds[cycle % len(kinds):] + kinds[:cycle % len(kinds)]
                        for kind in ordered:
                            if len(running) >= capacity:
                                break
                            if kind in due and (realtime, kind) not in running:
                                future = executor.submit(self.run_batch, kind, realtime=realtime, cold=cycle % 5 == 0)
                                futures[future] = (realtime, kind)
                                running.append((realtime, kind))
                                future.add_done_callback(lambda _: self._wake.set())
                    cycle += 1
                except Exception:
                    self._counts["storage_errors"] += 1
                    self._retry_until = self.now() + 5
                    logger.warning("Personnel scheduler degraded; will retry without discarding archive")
                self._wake.clear()
                self._wake.wait(1)


class PersonnelResolver(EsiResolver):
    """Archive reads are nonblocking; missing data is filled by the runtime."""

    def __init__(self, original: EsiResolver, runtime: PersonnelRuntime):
        super().__init__(client=original.client, cache=original.cache)
        self.runtime = runtime
        self.personnel_enabled = True
        self.original = original

    def cached_name(self, name: str, *, allow_stale: bool = False):
        self.runtime.request(name, seen_at=0)
        row = self.runtime.lookup(name)
        return (ResolvedName(row["name"], "character", row["character_id"]), "cached") if row else (None, "miss")

    def resolve_names(self, names: list[str]) -> list[ResolvedName]:
        """Preserve synchronous public/admin lookup semantics outside OCR ingestion."""
        return self.original.resolve_names(names)

    def enrich_observation(self, observation):
        result = []
        for name in observation.names:
            resolved, _ = self.cached_name(name, allow_stale=True)
            if resolved:
                result.append(resolved)
            else:
                # Preserve already cached non-character names, e.g. map system names.
                legacy, _ = super().cached_name(name, allow_stale=True)
                if legacy and legacy.category != "character":
                    result.append(legacy)
        observation.character_ids = list(dict.fromkeys([*observation.character_ids,
                                                        *(item.entity_id for item in result if item.category == "character")]))
        observation.metadata = self._resolution_metadata(observation, {item.name.casefold() for item in result},
                                                         observation.system_id is not None)
        return observation

    def cached_character_profile(self, character_id: int, *, allow_stale: bool = False):
        return self.runtime.profile(character_id)

    def character_profile(self, character_id: int):
        return self.runtime.profile(character_id) or {"character_id": character_id}

    def cache_snapshot(self):
        return {**super().cache_snapshot(), "archive": self.runtime.snapshot()}
