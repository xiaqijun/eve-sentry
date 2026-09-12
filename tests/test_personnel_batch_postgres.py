"""Real PostgreSQL batch operations and additive expiry migration."""

# ruff: noqa: F811 -- shared fixtures require matching parameter names.
from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate
from tests.test_personnel_postgres import postgres_archive, postgres_dsn  # noqa: F401


def test_postgres_batch_promotions_seen_times_and_due_lanes(postgres_archive):
    archive, _ = postgres_archive
    with archive.batch():
        for cid in range(1, 101):
            archive.save_identity(IdentityUpdate(cid, f"Pilot {cid}", 100, 100))
    archive.request_refresh_many([("affiliation", cid, 5, 100) for cid in range(1, 101)])
    archive.request_refresh_many([("affiliation", cid, 1, 90) for cid in range(1, 51)])
    archive.observe_many([(cid, 200) for cid in range(1, 101)])
    assert archive.due_work(100) == [
        {"kind": "affiliation", "priority": 0}, {"kind": "affiliation", "priority": 2}]
    rows = archive.get_profiles(list(range(1, 101)))
    assert all(row["last_seen_at"] == 200 for row in rows.values())
    assert len(archive.claim(now=100, limit=100, maximum_priority=1)) == 50


def test_postgres_expiry_migration_is_idempotent_and_keeps_profiles(postgres_archive):
    archive, factory = postgres_archive
    archive.save_identity(IdentityUpdate(1, "Pilot", 100, 100))
    # Reproduce the previous schema in this test's isolated, disposable namespace.
    with factory() as connection:
        connection.execute("ALTER TABLE personnel_profiles DROP COLUMN affiliation_expires_at")
    archive.migrate()
    archive.migrate()
    archive.save_affiliation(AffiliationUpdate(1, 10, 101, expires_at=120))
    assert archive.get_profiles([1])[1]["affiliation_expires_at"] == 120
    assert archive.get_profiles([1])[1]["name"] == "Pilot"
