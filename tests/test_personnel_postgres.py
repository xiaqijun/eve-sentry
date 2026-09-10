"""Real PostgreSQL transaction and row-lock tests in isolated disposable schemas."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate, PersonnelArchive


DSN = os.environ.get("EVE_SENTRY_TEST_POSTGRES_DSN", "").strip()
if not DSN:
    pytest.skip("EVE_SENTRY_TEST_POSTGRES_DSN is not configured", allow_module_level=True)

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


@pytest.fixture
def postgres_dsn():
    schema = "eve_sentry_personnel_test_" + uuid4().hex
    isolated_dsn = make_conninfo(
        DSN, options=f"-csearch_path={schema} -clock_timeout=2000 -cstatement_timeout=5000",
    )
    with psycopg.connect(DSN) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        # ConnectionInfo.dsn omits passwords; consumers must reuse this input.
        yield isolated_dsn
    finally:
        # Only the exact random schema created above can be removed.
        with psycopg.connect(DSN) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def postgres_archive(postgres_dsn):
    factory = lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    archive = PersonnelArchive(factory)
    archive.migrate()
    yield archive, factory


def test_postgres_personnel_persistence_and_fenced_completion(postgres_archive):
    archive, factory = postgres_archive
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    archive.request_refresh("affiliation", 1, priority=1, due_at=100)
    old = archive.claim(now=100, lease_seconds=10)[0]
    current = archive.claim(now=111)[0]
    assert not archive.finish(
        old, now=112, next_due_at=500, next_priority=2,
        update=AffiliationUpdate(1, 9, 112),
    )
    assert archive.finish(
        current, now=112, next_due_at=500, next_priority=2,
        update=AffiliationUpdate(1, 10, 112),
    )
    reopened = PersonnelArchive(factory)
    assert reopened.find_names(["PILOT"])["pilot"]["corporation_id"] == 10
    assert reopened.get_profiles([1])[1]["alliance_id"] is None
    assert not reopened.claim(now=113)


def test_postgres_claim_skips_locked_jobs(postgres_archive):
    archive, factory = postgres_archive
    for character_id in (1, 2):
        archive.request_refresh("identity", character_id, priority=1, due_at=100)
    with factory() as first_worker:
        first_worker.execute(
            "SELECT * FROM personnel_refresh_jobs WHERE entity_key = %s FOR UPDATE", ("1",),
        ).fetchone()
        # A separate connection must return the other row without waiting for this lock.
        other = archive.claim(now=100)
        assert [lease.entity_key for lease in other] == ["2"]
    remaining = archive.claim(now=100)
    assert [lease.entity_key for lease in remaining] == ["1"]


def test_postgres_result_and_acknowledgment_roll_back_together(postgres_archive):
    archive, factory = postgres_archive
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    archive.request_refresh("identity", 1, priority=1, due_at=100)
    lease = archive.claim(now=100)[0]
    with factory() as connection:
        connection.execute(
            "ALTER TABLE personnel_refresh_jobs ADD CONSTRAINT test_reject_ack CHECK (next_due_at < 500)",
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        archive.finish(
            lease, now=101, next_due_at=500, next_priority=2,
            update=IdentityUpdate(1, "Renamed Pilot", 101, 101),
        )
    assert archive.find_names(["Pilot"])["pilot"]["name"] == "Pilot"
    assert archive.find_names(["Renamed Pilot"]) == {}
    assert archive.finish(lease, now=102, next_due_at=400, next_priority=2)
