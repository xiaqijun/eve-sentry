"""Deterministic M1 repository and refresh-policy contracts."""

from contextlib import contextmanager
import sqlite3

import pytest

from app.esi.personnel_archive import (
    AffiliationUpdate, IdentityUpdate, NameConflict, OrganizationUpdate, PersonnelArchive,
)
from app.esi.personnel_policy import (
    DAY, NAME_REFRESH_SECONDS, RefreshLoad, background_slots, name_key,
    personnel_tier, refresh_due_at, retry_delay,
)


def test_missing_identity_cannot_acknowledge_affiliation_job(archive_factory):
    archive = archive_factory()
    archive.request_refresh("affiliation", 1, priority=1, due_at=100)
    lease = archive.claim(now=100)[0]
    with pytest.raises(ValueError, match="confirmed identity"):
        archive.finish(lease, now=101, next_due_at=200, next_priority=1,
                       update=AffiliationUpdate(1, 10, 101))
    archive.save_identity(IdentityUpdate(1, "Pilot", 101, 101))
    assert archive.finish(lease, now=102, next_due_at=200, next_priority=1,
                          update=AffiliationUpdate(1, 10, 102))


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_claim_and_finish_reject_non_integer_schedule(archive_factory, value):
    archive = archive_factory()
    with pytest.raises(ValueError):
        archive.claim(now=100, limit=value)
    with pytest.raises(ValueError):
        archive.claim(now=100, minimum_priority=value)
    archive.request_refresh("identity", 1, priority=1, due_at=100)
    lease = archive.claim(now=100)[0]
    with pytest.raises(ValueError):
        archive.finish(lease, now=101, next_due_at=200, next_priority=value)


class SqliteConnection:
    """The production repository uses psycopg-style placeholders/mapping rows."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=()):
        return self.connection.execute(query.replace("%s", "?"), params)


@pytest.fixture
def archive_factory(tmp_path):
    path = tmp_path / "personnel.sqlite"

    @contextmanager
    def connection():
        database = sqlite3.connect(path)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("BEGIN IMMEDIATE")
        try:
            yield SqliteConnection(database)
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()

    def factory():
        return PersonnelArchive(connection, dialect="sqlite")

    factory().migrate()
    return factory


def test_identity_survives_reopen_rename_and_stale_results(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Old Name", 100, 90))
    assert archive.find_names([" OLD NAME "])["old name"]["character_id"] == 1
    archive.save_identity(IdentityUpdate(1, "New Name", 200, 180))
    reopened = archive_factory()
    assert reopened.find_names(["old name"]) == {}
    assert reopened.find_names(["new name"])["new name"]["name"] == "New Name"
    assert not reopened.save_identity(IdentityUpdate(1, "Old Name", 150, 195))
    assert not reopened.save_identity(IdentityUpdate(1, "Wrong Name", 200, 196))
    row = reopened.get_profiles([1])[1]
    assert row["name"] == "New Name"
    assert row["last_seen_at"] == 196
    assert row["first_seen_at"] == 90
    assert row["revision"] == 2
    assert name_key("Name  With Spaces") != name_key("Name With Spaces")


def test_name_conflict_rolls_back_both_profile_and_index(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 90))
    with pytest.raises(NameConflict):
        archive.save_identity(IdentityUpdate(2, "PILOT", 200, 190))
    assert archive.get_profiles([2]) == {}
    assert archive.find_names(["Pilot"])["pilot"]["character_id"] == 1


def test_affiliation_changes_do_not_refresh_name_or_keep_old_alliance(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 90))
    assert archive.save_affiliation(AffiliationUpdate(1, 10, 200, alliance_id=20))
    assert archive.save_affiliation(AffiliationUpdate(1, 11, 300))
    assert not archive.save_affiliation(AffiliationUpdate(1, 10, 200, alliance_id=20))
    assert not archive.save_affiliation(AffiliationUpdate(1, 12, 300))
    row = archive.get_profiles([1])[1]
    assert row["corporation_id"] == 11
    assert row["alliance_id"] is None
    assert row["faction_id"] is None
    assert row["name_checked_at"] == 100
    assert row["affiliation_fetched_at"] == 300
    version = row["revision"]
    assert archive.save_affiliation(AffiliationUpdate(1, 11, 400))
    assert archive.get_profiles([1])[1]["revision"] == version


def test_organizations_are_shared_persistent_and_separated_by_kind(archive_factory):
    archive = archive_factory()
    archive.save_organization(OrganizationUpdate("corporation", 10, "Corp", 100))
    archive.save_organization(OrganizationUpdate("alliance", 10, "Alliance", 100))
    assert not archive.save_organization(OrganizationUpdate("corporation", 10, "Old", 90))
    assert archive_factory().get_organizations("corporation", [10, 10])[10]["name"] == "Corp"
    assert archive.get_organizations("alliance", [10])[10]["name"] == "Alliance"


def test_inflight_promotion_is_not_lost_on_completion(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 90))
    archive.request_refresh("affiliation", 1, priority=5, due_at=100)
    lease = archive.claim(now=100)[0]
    assert archive.claim(now=101) == []
    archive.request_refresh("affiliation", 1, priority=1, due_at=102)
    assert archive.finish(lease, now=103, next_due_at=10000, next_priority=5,
                          update=AffiliationUpdate(1, 10, 103))
    next_lease = archive_factory().claim(now=104, maximum_priority=1)[0]
    assert next_lease.revision > lease.revision
    assert not archive.finish(lease, now=105, next_due_at=10000, next_priority=5,
                              update=AffiliationUpdate(1, 999, 105))
    assert archive.get_profiles([1])[1]["corporation_id"] == 10


def test_expired_lease_cannot_write_and_retry_keeps_successful_data(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 90))
    archive.save_affiliation(AffiliationUpdate(1, 10, 100))
    archive.request_refresh("affiliation", 1, priority=1, due_at=100)
    first = archive.claim(now=100, lease_seconds=5)[0]
    second = archive_factory().claim(now=106)[0]
    assert not archive.finish(first, now=107, next_due_at=1000, next_priority=1,
                              update=AffiliationUpdate(1, 999, 107))
    assert archive.fail(second, now=108, error_code="throttled", retry_after=60)
    assert archive.claim(now=167) == []
    retried = archive_factory().claim(now=168)[0]
    assert retried.failures == 1
    assert archive.get_profiles([1])[1]["corporation_id"] == 10
    assert not archive.fail(second, now=169, error_code="timeout")


def test_result_mismatch_and_name_conflict_do_not_acknowledge_job(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 90))
    archive.save_identity(IdentityUpdate(2, "Other", 100, 90))
    archive.request_refresh("identity", 2, priority=1, due_at=100)
    lease = archive.claim(now=100)[0]
    with pytest.raises(ValueError, match="leased entity"):
        archive.finish(lease, now=101, next_due_at=200, next_priority=1,
                       update=IdentityUpdate(1, "Other", 101, 90))
    with pytest.raises(NameConflict):
        archive.finish(lease, now=102, next_due_at=200, next_priority=1,
                       update=IdentityUpdate(2, "Pilot", 102, 90))
    assert archive.get_profiles([2])[2]["name"] == "Other"
    assert archive.fail(lease, now=103, error_code="conflict")


def test_claim_lanes_and_oldest_first_reserve_cold_progress(archive_factory):
    archive = archive_factory()
    archive.request_refresh("resolve", " New Pilot ", priority=0, due_at=99)
    archive.request_refresh("affiliation", 1, priority=1, due_at=90)
    archive.request_refresh("affiliation", 2, priority=5, due_at=1)
    assert archive.claim(now=100, maximum_priority=0)[0].entity_key == "new pilot"
    cold = archive.claim(now=100, minimum_priority=1, oldest_first=True, limit=1)[0]
    assert cold.entity_key == "2"
    assert archive.claim(now=100, minimum_priority=1)[0].entity_key == "1"


def test_job_context_is_part_of_identity_and_success_reschedules(archive_factory):
    archive = archive_factory()
    for context in ("a", "b"):
        archive.request_refresh("identity", 1, priority=1, due_at=100, context_key=context)
    leases = archive.claim(now=100)
    assert len(leases) == 2
    assert archive.finish(leases[0], now=101, next_due_at=200, next_priority=5)
    assert archive.claim(now=150)  # The other lease expired and can be recovered.
    assert archive.claim(now=200, minimum_priority=5)[0].context_key == leases[0].context_key


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "123"])
def test_archive_rejects_invalid_ids(archive_factory, value):
    with pytest.raises(ValueError):
        archive_factory().save_identity(IdentityUpdate(value, "Pilot", 1, 1))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_archive_rejects_invalid_timestamps(archive_factory, value):
    with pytest.raises(ValueError):
        archive_factory().save_identity(IdentityUpdate(1, "Pilot", value, 1))


def test_archive_bounds_batches_and_validates_jobs(archive_factory):
    archive = archive_factory()
    assert archive.get_profiles([]) == archive.find_names([]) == {}
    with pytest.raises(ValueError):
        archive.get_profiles(list(range(1, 1002)))
    with pytest.raises(ValueError):
        archive.claim(now=100, limit=1001)
    with pytest.raises(ValueError):
        archive.claim(now=100, lease_seconds=0)
    with pytest.raises(ValueError):
        archive.request_refresh("invalid", 1, priority=1, due_at=100)
    with pytest.raises(ValueError):
        archive.save_organization(OrganizationUpdate("faction", 1, "X", 100))


@pytest.mark.parametrize("age,tier", [(0, 2), (DAY, 2), (DAY+1, 3), (7*DAY, 3),
                                     (7*DAY+1, 4), (30*DAY, 4), (30*DAY+1, 5)])
def test_refresh_tiers(age, tier):
    assert personnel_tier(now=100*DAY, last_seen_at=100*DAY-age) == tier
    assert personnel_tier(now=100*DAY, last_seen_at=100*DAY-age, active=True) == 1


def test_idle_refresh_respects_upstream_and_names_are_low_frequency():
    normal = refresh_due_at(character_id=1, fetched_at=100, tier=1)
    early = refresh_due_at(character_id=1, fetched_at=100, tier=1, spare_capacity=True)
    assert early == normal == 3700
    assert refresh_due_at(character_id=1, fetched_at=100, tier=1,
                          spare_capacity=True, upstream_valid_until=86500) == 3700
    assert NAME_REFRESH_SECONDS == 90*DAY


def test_adaptive_policy_never_expands_background_under_pressure():
    assert background_slots(1, RefreshLoad()) == 2
    assert background_slots(4, RefreshLoad()) == 4
    assert background_slots(4, RefreshLoad(realtime_pending=1)) == 0
    assert background_slots(4, RefreshLoad(throttled=True)) == 0
    assert background_slots(4, RefreshLoad(upstream_errors=True)) == 0
    assert background_slots(4, RefreshLoad(cpu_fraction=0.9)) == 3
    assert background_slots(1, RefreshLoad(database_wait_ms=50)) == 0
    assert background_slots(2, RefreshLoad(upstream_latency_ms=2000)) == 1
    assert background_slots(2, RefreshLoad(cpu_fraction=float("nan"))) == 0
    assert retry_delay(1000000, jitter=1) == 300
    assert retry_delay(1, retry_after=60) == 60
