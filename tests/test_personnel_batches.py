"""Batch transaction bounds, promotion fences, and rollback publication."""

# ruff: noqa: F811 -- pytest imports the shared archive fixture by name.

from contextlib import contextmanager

from app.esi.personnel_archive import IdentityUpdate
from app.esi.personnel_runtime import PersonnelRuntime
from tests.test_personnel_archive import archive_factory  # noqa: F401
from tests.test_personnel_runtime import Client


def test_hundred_identity_results_use_bounded_transactions(archive_factory):
    archive = archive_factory()
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1000)
    runtime.set_active([(None, f"Pilot {cid}", 1000) for cid in range(1, 101)])
    runtime.drain_requests()
    calls = []
    original = archive._connection.factory

    @contextmanager
    def connection():
        calls.append(1)
        with original() as value:
            yield value

    archive._connection.factory = connection
    assert runtime.run_batch("resolve", realtime=True) == 100
    assert len(calls) <= 8
    assert len(archive.get_profiles(list(range(1, 101)))) == 100


def test_result_batch_rollback_never_publishes_hot_rows(archive_factory, monkeypatch):
    archive = archive_factory()
    runtime = PersonnelRuntime(archive, Client(), now=lambda: 1000)
    runtime.set_active([(None, "Pilot 1", 1000), (None, "Pilot 2", 1000)])
    runtime.drain_requests()
    original = archive.finish

    def fail_second(lease, **kwargs):
        if lease.entity_key == "pilot 2":
            raise ValueError("reject second result")
        return original(lease, **kwargs)

    monkeypatch.setattr(archive, "finish", fail_second)
    runtime.run_batch("resolve", realtime=True)
    assert archive.get_profiles([1, 2]) == {}
    assert runtime.lookup("Pilot 1") is None
    assert runtime.snapshot().get("refresh_success", 0) == 0


def test_bulk_promotion_preserves_retry_and_inflight_lease(archive_factory):
    archive = archive_factory()
    archive.request_refresh_many([("affiliation", 1, 5, 100), ("affiliation", 2, 5, 100)])
    leases = archive.claim(now=100, limit=2)
    archive.fail(leases[0], now=101, error_code="throttled", retry_after=600)
    archive.request_refresh_many([("affiliation", 1, 1, 102), ("affiliation", 2, 1, 102)], promote_only=True)
    assert archive.claim(now=103) == []
    assert archive.due_work(701)


def test_bulk_sightings_never_move_last_seen_backwards(archive_factory):
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 200))
    archive.save_identity(IdentityUpdate(2, "Pilot 2", 100, 200))
    archive.observe_many([(1, 150), (2, 250), (2, 220)])
    rows = archive.get_profiles([1, 2])
    assert rows[1]["last_seen_at"] == 200
    assert rows[2]["last_seen_at"] == 250
