"""Expired affiliation gets a bounded realtime refresh, not tomorrow's deadline."""

import pytest

from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate
from app.esi.personnel_runtime import PersonnelRuntime
from tests.test_personnel_archive import archive_factory  # noqa: F401
from tests.test_personnel_runtime import Client


def setup_stale(archive_factory, *, active=True, fetched=100):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, fetched))
    archive.request_refresh("affiliation", 1, priority=1, due_at=86500)
    client = Client()
    client.response_freshness = lambda: {"1": {"fetched_at": 100, "expires_at": 86500}}
    clock = [10000.0]
    runtime = PersonnelRuntime(archive, client, now=lambda: clock[0])
    if active:
        runtime.set_active([(1, "Pilot 1", clock[0])])
    else:
        runtime.request("Pilot 1", seen_at=100)
    runtime._upstream_until[1] = 86500
    return archive, runtime, clock


def test_active_stale_repairs_existing_priority_one_deadline(archive_factory):
    archive, runtime, clock = setup_stale(archive_factory)
    runtime.drain_requests()
    assert runtime.profile(1)["affiliation_trusted"] is False
    jobs = archive.claim(now=clock[0], kind="affiliation", maximum_priority=1)
    assert len(jobs) == 1
    # A repeated sighting cannot steal an already running lease.
    runtime.set_active([(1, "Pilot 1", clock[0])])
    runtime.drain_requests()
    assert archive.claim(now=clock[0], kind="affiliation") == []
    assert archive.finish(jobs[0], now=clock[0], next_due_at=clock[0] + 300, next_priority=1,
                          update=AffiliationUpdate(1, 10, clock[0]))


@pytest.mark.parametrize("active,fetched", [(False, 9900), (True, 9900)])
def test_cold_or_trusted_profiles_do_not_force_refresh(archive_factory, active, fetched):
    archive, runtime, clock = setup_stale(archive_factory, active=active, fetched=fetched)
    runtime.drain_requests()
    assert archive.claim(now=clock[0], kind="affiliation") == []
    assert not archive.claim(now=fetched + 3599, kind="affiliation")
    assert len(archive.claim(now=fetched + 3600, kind="affiliation")) == 1


def test_stale_refresh_preserves_failed_job_backoff(archive_factory):
    archive, runtime, clock = setup_stale(archive_factory)
    archive.expedite_stale_affiliation(1, now=clock[0])
    lease = archive.claim(now=clock[0], kind="affiliation")[0]
    archive.fail(lease, now=clock[0], error_code="throttled", retry_after=180)
    runtime.drain_requests()
    assert not archive.claim(now=clock[0] + 179, kind="affiliation")
    assert len(archive.claim(now=clock[0] + 180, kind="affiliation")) == 1


def test_still_old_gateway_response_retries_after_sixty_seconds(archive_factory):
    archive, runtime, clock = setup_stale(archive_factory)
    runtime.drain_requests()
    assert runtime.run_batch("affiliation", realtime=True) == 1
    assert runtime.profile(1)["affiliation_trusted"] is False
    for offset in (1, 10, 59):
        clock[0] = 10000 + offset
        runtime.set_active([(1, "Pilot 1", clock[0])])
        runtime.drain_requests()
        assert not archive.claim(now=clock[0], kind="affiliation")
    clock[0] = 10060
    runtime.client.response_freshness = lambda: {"1": {"fetched_at": clock[0], "expires_at": clock[0] + 300}}
    assert runtime.run_batch("affiliation", realtime=True) == 1
    assert runtime.profile(1)["affiliation_trusted"] is True
    assert archive.get_profiles([1])[1]["affiliation_fetched_at"] == clock[0]
