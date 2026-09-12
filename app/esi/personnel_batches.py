"""Bounded SQL batches shared by foreground promotion and background maintenance."""

from app.esi.personnel_archive import (
    AffiliationUpdate,
    IdentityUpdate,
    OrganizationUpdate,
)
from app.esi.personnel_policy import (
    AFFILIATION_TTL,
    NAME_REFRESH_SECONDS,
    name_key,
    personnel_tier,
    refresh_due_at,
)


def schedule_profiles(runtime, rows):
    if not rows:
        return
    now = runtime.now()
    jobs, organizations = [], {"corporation": set(), "alliance": set()}
    with runtime._lock:
        active_ids, active_names = set(runtime._active), set(runtime._active_names)
    with runtime.archive.batch():
        for row in rows:
            cid = row["character_id"]
            tier = personnel_tier(now=now, last_seen_at=row["last_seen_at"],
                                  active=cid in active_ids or name_key(row["name"]) in active_names)
            fetched = float(row.get("affiliation_fetched_at") or 0)
            due = refresh_due_at(character_id=cid, fetched_at=fetched, tier=tier,
                                 upstream_valid_until=row.get("affiliation_expires_at") or runtime._upstream_until.get(cid, 0)) if fetched else now
            jobs.extend((("affiliation", cid, tier, due),
                         ("identity", cid, tier, float(row["name_checked_at"]) + NAME_REFRESH_SECONDS)))
            # Repair old schedules while preserving leases and Retry-After.
            runtime.archive.expedite_stale_affiliation(cid, now=now, due_at=due)
            for kind, organization_ids in organizations.items():
                if row.get(kind + "_id"):
                    organization_ids.add(row[kind + "_id"])
        runtime.archive.request_refresh_many(jobs, promote_only=True)
        organization_jobs = []
        for kind, ids in organizations.items():
            existing = runtime.archive.get_organizations(kind, list(ids)) if ids else {}
            for entity_id in ids:
                row = existing.get(entity_id)
                due = float(row["fetched_at"]) + NAME_REFRESH_SECONDS if row else now
                organization_jobs.append((kind, entity_id, 5 if row else 2, due))
        runtime.archive.request_refresh_many(organization_jobs)


def commit_refresh_batch(runtime, kind, leases, rows, freshness):
    """Commit one bounded result batch before publishing any hot-cache updates."""
    archive, now = runtime.archive, runtime.now
    ids = ([int(row["id"]) for row in rows.values()] if kind in {"resolve", "identity"}
           else [int(lease.entity_key) for lease in leases] if kind == "affiliation" else [])
    profiles = archive.get_profiles(ids) if ids else {}
    completed, missing, late = [], [], 0
    with archive.batch():
        for lease in leases:
            row = rows.get(lease.entity_key)
            if not row:
                archive.fail(lease, now=now(), error_code="not_found", retry_after=60)
                if kind == "resolve":
                    missing.append(lease.entity_key)
                continue
            fetched, expires = freshness[lease.entity_key]
            cid, priority = None, 2
            if kind in {"resolve", "identity"}:
                cid = int(row["id"])
                existing = profiles.get(cid)
                with runtime._lock:
                    seen = max(existing["last_seen_at"] if existing else 0,
                               runtime._sightings.get(name_key(row["name"]), 0))
                update = IdentityUpdate(cid, row["name"], fetched, seen)
                due = max(now() + 60, fetched + NAME_REFRESH_SECONDS, expires)
                if kind == "resolve":
                    due = now() + 36500 * 86400
            elif kind == "affiliation":
                cid = int(lease.entity_key)
                profile = profiles[cid]
                update = AffiliationUpdate(cid, int(row["corporation_id"]), fetched,
                                           row.get("alliance_id"), row.get("faction_id"), expires or None)
                with runtime._lock:
                    active = cid in runtime._active or name_key(profile["name"]) in runtime._active_names
                priority = personnel_tier(now=now(), last_seen_at=profile["last_seen_at"], active=active)
                due = max(now() + 60, refresh_due_at(character_id=cid, fetched_at=fetched,
                                                    tier=priority, upstream_valid_until=expires))
                if not fetched or now() - fetched >= AFFILIATION_TTL:
                    due = now() + 60
            else:
                update = OrganizationUpdate(kind, int(lease.entity_key), row["name"], fetched)
                # Low-priority name validation; names remain usable while old.
                due = now() + NAME_REFRESH_SECONDS
            if archive.finish(lease, now=now(), next_due_at=due, next_priority=priority, update=update):
                completed.append((cid, expires, lease.entity_key))
            else:
                late += 1
    # Transaction succeeded. A rollback must never leave an uncommitted hot copy.
    runtime._counts["refresh_success"] += len(completed)
    runtime._counts["late_results"] += late
    with runtime._lock:
        for key in missing:
            runtime._negative[key] = now() + 60
        while len(runtime._negative) > runtime.max_hot:
            runtime._negative.popitem(last=False)
        if kind == "affiliation":
            runtime._upstream_until.update({cid: expires for cid, expires, _ in completed if cid})
        changed_ids = {cid for cid, _, _ in completed if cid}
        if kind in {"corporation", "alliance"}:
            changed_orgs = {int(key) for _, _, key in completed}
            changed_ids.update(cid for cid, p in runtime._profiles.items() if p.get(kind + "_id") in changed_orgs)
    changed_ids = sorted(changed_ids)
    for start in range(0, len(changed_ids), 500):
        changed = list(archive.get_profiles(changed_ids[start:start + 500]).values())
        runtime._remember(changed)
        if kind in {"resolve", "identity"}:
            schedule_profiles(runtime, changed)
