"""Tests for opt-in historical report retention."""

from app.core.active_intel import ActiveIntelItem
from app.server.intel_store import IntelStore


NOW = "2026-07-30T12:00:00+00:00"
OLD = "2026-06-01T12:00:00+00:00"
RECENT = "2026-07-20T12:00:00+00:00"


def _add_observation(store, name: str, received_at: str) -> str:
    observation = store.add_observation(
        {
            "source": "manual",
            "system_name": "Tama",
            "names": [name],
            "seen_at": received_at,
            "received_at": received_at,
        }
    )
    return observation.observation_id


def test_json_retention_is_disabled_at_zero_and_persists_pruning(tmp_path) -> None:
    path = tmp_path / "intel.json"
    store = IntelStore(path)
    old_id = _add_observation(store, "Old Pilot", OLD)
    recent_id = _add_observation(store, "Recent Pilot", RECENT)

    assert store.prune_reports_older_than(0, now=NOW) == 0
    assert {item["id"] for item in store.list_reports()} == {old_id, recent_id}
    assert store.prune_reports_older_than(30, now=NOW) == 1
    assert [item["id"] for item in store.list_reports()] == [recent_id]
    store.close()

    reopened = IntelStore(path)
    try:
        assert [item["id"] for item in reopened.list_reports()] == [recent_id]
    finally:
        reopened.close()


def test_retention_keeps_reports_referenced_by_active_intel(tmp_path) -> None:
    store = IntelStore(tmp_path / "intel.json")
    old_id = _add_observation(store, "Active Pilot", OLD)
    store._active_intel["active:test"] = ActiveIntelItem(
        active_id="active:test",
        source="manual",
        source_instance="test",
        system_name="Tama",
        name="Active Pilot",
        source_observation_ids=[old_id],
    )
    try:
        assert store.prune_reports_older_than(30, now=NOW) == 0
        assert [item["id"] for item in store.list_reports()] == [old_id]
    finally:
        store.close()


def test_retention_restores_memory_when_persistence_fails(tmp_path) -> None:
    class FailingStore(IntelStore):
        def _persist_pruned_reports(self, report_ids):
            raise OSError("disk unavailable")

    store = FailingStore(tmp_path / "intel.json")
    old_id = _add_observation(store, "Old Pilot", OLD)
    try:
        try:
            store.prune_reports_older_than(30, now=NOW)
        except OSError as exc:
            assert str(exc) == "disk unavailable"
        else:
            raise AssertionError("expected persistence failure")
        assert [item["id"] for item in store.list_reports()] == [old_id]
    finally:
        store.close()
