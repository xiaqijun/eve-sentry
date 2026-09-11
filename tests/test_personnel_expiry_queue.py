"""All expired affiliations are eligible; activity only orders the work."""

import pytest

from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate
from app.esi.personnel_policy import DAY, refresh_due_at
from app.esi.personnel_runtime import PersonnelRuntime
from tests.test_personnel_archive import archive_factory  # noqa: F401
from tests.test_personnel_runtime import Client


@pytest.mark.parametrize("tier", range(1, 6))
@pytest.mark.parametrize("spare", [False, True])
def test_every_tier_uses_official_expiry(tier, spare):
    assert refresh_due_at(character_id=tier, fetched_at=100, tier=tier,
                          spare_capacity=spare, upstream_valid_until=86500) == 3700


def test_all_expired_tiers_are_queued_and_repeat_without_new_ocr(archive_factory):
    archive = archive_factory()
    clock = [100 * DAY]
    client = Client()
    client.response_freshness = lambda: {
        str(cid): {"fetched_at": clock[0], "expires_at": clock[0] + 3600} for cid in range(1, 6)
    }
    client.get_character_affiliations = lambda ids: [
        {"character_id": cid, "corporation_id": 10} for cid in ids
    ]
    for cid, age in enumerate([0, 1, 2 * DAY, 10 * DAY, 40 * DAY], start=1):
        archive.save_identity(IdentityUpdate(cid, f"Pilot {cid}", clock[0], clock[0] - age))
        archive.save_affiliation(AffiliationUpdate(cid, 10, clock[0] - 7200))
        # Persisted old schedules must be repaired, even when priority stays equal.
        archive.request_refresh("affiliation", cid, priority=cid, due_at=clock[0] + 30 * DAY)
    runtime = PersonnelRuntime(archive, client, now=lambda: clock[0])
    runtime.set_active([(1, "Pilot 1", clock[0])])
    runtime.drain_requests()
    runtime.maintenance()
    assert runtime.run_batch("affiliation", realtime=True) == 1
    assert runtime.run_batch("affiliation", realtime=False) == 4
    for cid in range(1, 6):
        assert runtime.profile(cid)["cache_status"] == "cached"
    clock[0] += 3599
    assert runtime.run_batch("affiliation", realtime=True) == 0
    assert runtime.run_batch("affiliation", realtime=False) == 0
    clock[0] += 1
    assert runtime.profile(1)["affiliation_trusted"] is False
    # No OCR, maintenance or manual button: successful jobs requeue themselves.
    assert runtime.run_batch("affiliation", realtime=True) == 1
    assert runtime.run_batch("affiliation", realtime=False) == 4
    assert all(runtime.profile(cid)["affiliation_trusted"] for cid in range(1, 6))


def test_due_queue_orders_priority_then_age_and_excludes_unexpired(archive_factory):
    archive = archive_factory()
    for cid, priority, due in [(1, 5, 10), (2, 2, 50), (3, 2, 20), (4, 1, 101)]:
        archive.request_refresh("affiliation", cid, priority=priority, due_at=due)
    assert [lease.entity_key for lease in archive.claim(now=100, kind="affiliation")] == ["3", "2", "1"]


def test_cold_expiry_repair_preserves_backoff_and_lease(archive_factory):
    archive = archive_factory()
    now = 100 * DAY
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, now - 7200))
    archive.request_refresh("affiliation", 1, priority=5, due_at=now)
    runtime = PersonnelRuntime(archive, Client(), now=lambda: now)
    lease = archive.claim(now=now, kind="affiliation")[0]
    runtime.maintenance()
    assert not archive.claim(now=now, kind="affiliation")
    assert archive.fail(lease, now=now, error_code="throttled", retry_after=180)
    runtime.maintenance()
    assert not archive.claim(now=now + 179, kind="affiliation")
    assert len(archive.claim(now=now + 180, kind="affiliation")) == 1
