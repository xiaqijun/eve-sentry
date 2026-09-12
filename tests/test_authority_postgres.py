"""Full PostgreSQL store: primary clear, restart, durable stream and snapshot."""

import pytest

from app.server.postgres_store import PostgreSQLIntelStore
from tests.test_personnel_postgres import postgres_dsn  # noqa: F401


@pytest.fixture
def store(postgres_dsn):  # noqa: F811
    store = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[])
    try:
        yield store
    finally:
        store.close()


def presence(store, client, count, **extra):
    return store.record_hostile_presence(
        {
            "client_id": client,
            "system_name": "S-KSWL",
            "hostile_icon_count": count,
            **extra,
        }
    )


def test_primary_clear_is_identical_in_event_and_bootstrap_and_survives_restart(
    store,
    postgres_dsn,  # noqa: F811 -- imported shared pytest fixture
):
    presence(store, "first", 1)
    presence(store, "standby", 9)
    events = store.list_intel_event_page()
    assert len(events) == 1
    assert events[0]["payload"]["hostile_count"] == 1
    assert events[0]["payload"]["primary_client_id"] == "first"
    items, _, version = store.read_active_event_snapshot()
    assert version == events[-1]["seq"]
    assert items[0]["metadata"]["hostile_icon_count"] == 1
    presence(store, "first", 0)
    assert store.read_active_event_snapshot()[0] == []
    events = store.list_intel_event_page()
    assert events[-1]["event_type"] == "alert.cleared"
    store.close()
    reopened = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[])
    try:
        assert reopened.read_active_event_snapshot()[0] == []
        assert reopened.list_intel_event_page() == events
        presence(reopened, "first", 2)
        assert reopened.list_intel_event_page()[-1]["payload"]["hostile_count"] == 2
    finally:
        reopened.close()


def test_capture_loss_retains_unknown_until_valid_clear_and_does_not_repeat_clear(
    store,
):
    import time
    from datetime import datetime, timezone

    from app.server.capture_state import CAPTURE_LEASE_SECONDS
    from tests.test_capture_state import frame

    presence(store, "first", 1, capture=frame(1))
    now = time.time() + CAPTURE_LEASE_SECONDS + 1
    store._stale_heartbeat_cleanup_after = 0
    store.expire_active_intel(datetime.fromtimestamp(now, timezone.utc).isoformat())
    event = store.list_intel_event_page()[-1]
    assert event["event_type"] == "alert.updated"
    assert event["payload"]["hostile_count"] == 1
    assert event["payload"]["freshness"] == "unknown"
    assert (
        store.read_active_event_snapshot()[0][0]["metadata"]["freshness"] == "unknown"
    )
    presence(store, "first", 0, capture=frame(2))
    assert store.read_active_event_snapshot()[0] == []
    before = store.list_intel_event_page()
    assert before[-1]["event_type"] == "alert.cleared"
    presence(store, "first", 0, capture=frame(3))
    assert store.list_intel_event_page() == before


def test_zero_only_primary_is_persisted_once_without_enemy_event(store):
    presence(store, "first", 0)
    first = store.list_intel_event_page()
    assert len(first) == 1
    assert first[0]["event_type"] == "alert.cleared"
    assert store.read_active_event_snapshot()[0] == []
    presence(store, "first", 0)
    assert store.list_intel_event_page() == first


def test_query_cannot_move_primary_or_publish_automatic_enemy(store):
    presence(store, "first", 1)
    before = store.list_intel_event_page()
    store.record_ocr_snapshot(
        {
            "client_id": "first",
            "system_name": "Jita",
            "names": ["Query Pilot"],
            "query_id": "q1",
        }
    )
    assert store.list_intel_event_page() == before
    assert store._hostile_system_state()["s-kswl"]["primary_client_id"] == "first"
    rows = store.list_active_intel()
    query = next(row for row in rows if row["name"] == "Query Pilot")
    assert query["metadata"]["query_only"]
    assert query["metadata"]["client_id"].startswith("first:query:")


def test_zero_capture_loss_becomes_unknown_and_move_does_not_fake_clear(store):
    import time
    from datetime import datetime, timezone

    from app.server.capture_state import CAPTURE_LEASE_SECONDS
    from tests.test_capture_state import frame

    presence(store, "first", 0, capture=frame(1))
    store._stale_heartbeat_cleanup_after = 0
    now = datetime.fromtimestamp(
        time.time() + CAPTURE_LEASE_SECONDS + 1, timezone.utc
    ).isoformat()
    store.expire_active_intel(now)
    last = store.list_intel_event_page()[-1]
    assert last["payload"]["freshness"] == "unknown"
    assert last["payload"]["hostile_count"] == 0
    assert (
        store.read_active_event_snapshot()[0][0]["metadata"]["freshness"] == "unknown"
    )
    presence(store, "first", 2, capture=frame(2))
    store.record_hostile_presence(
        {
            "client_id": "first",
            "system_name": "Jita",
            "hostile_icon_count": 0,
            "capture": frame(3),
        }
    )
    old = [e for e in store.list_intel_event_page() if e["entity_key"] == "s-kswl"][-1]
    assert old["payload"]["freshness"] == "unknown"
    assert old["payload"]["hostile_count"] == 2


def test_failed_commit_is_repaired_without_another_client_upload(store, monkeypatch):
    store._state_maintenance.close()
    persist = store._persist_intel_events

    def fail(connection, events):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(store, "_persist_intel_events", fail)
    with pytest.raises(RuntimeError, match="injected"):
        presence(store, "first", 2)
    assert store.read_active_event_snapshot()[0] == []
    monkeypatch.setattr(store, "_persist_intel_events", persist)
    assert store._state_repair.run() == 1
    assert (
        store.read_active_event_snapshot()[0][0]["metadata"]["hostile_icon_count"] == 2
    )
    assert store._state_repair.run() == 0

    monkeypatch.setattr(store, "_persist_intel_events", fail)
    with pytest.raises(RuntimeError, match="injected"):
        presence(store, "first", 0)
    assert (
        store.read_active_event_snapshot()[0][0]["metadata"]["hostile_icon_count"] == 2
    )
    monkeypatch.setattr(store, "_persist_intel_events", persist)
    assert store._state_repair.run() == 1
    assert store.read_active_event_snapshot()[0] == []
    assert store.list_intel_event_page()[-1]["event_type"] == "alert.cleared"


def test_explicit_stop_is_not_a_visual_clear_and_zero_standby_can_take_over(store):
    presence(store, "first", 3)
    presence(store, "first", 0, source_status="stopped")
    last = store.list_intel_event_page()[-1]
    assert last["payload"]["freshness"] == "unknown"
    assert last["payload"]["hostile_count"] == 3
    presence(store, "standby", 0)
    assert store.list_intel_event_page()[-1]["event_type"] == "alert.cleared"
    assert (
        store.list_intel_event_page()[-1]["payload"]["primary_client_id"] == "standby"
    )


def test_stopped_heartbeat_cannot_clear_before_terminal_presence_arrives(store):
    from datetime import datetime, timedelta, timezone

    from app.core.active_intel import DEFAULT_OCR_GRACE_SECONDS

    store.record_heartbeat(
        {
            "client_id": "first",
            "client_type": "detector_client",
            "details": {"monitoring": True},
        }
    )
    presence(store, "first", 2)
    store.record_heartbeat(
        {
            "client_id": "first",
            "client_type": "detector_client",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )
    store.expire_active_intel(
        (
            datetime.now(timezone.utc)
            + timedelta(seconds=DEFAULT_OCR_GRACE_SECONDS + 1)
        ).isoformat()
    )
    assert store.list_intel_event_page()[-1]["payload"]["freshness"] == "unknown"
    assert store.list_intel_event_page()[-1]["payload"]["hostile_count"] == 2


def test_valid_detector_zero_does_not_clear_independent_channel_intel(store):
    from app.core.models import Observation

    store.add_observation(
        Observation(
            source="intel_channel",
            system_name="S-KSWL",
            raw_text="S-KSWL hostile",
            metadata={"hostile_count": 4},
        )
    )
    presence(store, "first", 0)
    assert store.list_intel_event_page()[-1]["payload"]["hostile_count"] == 4
    assert (
        store.read_active_event_snapshot()[0][0]["metadata"]["hostile_icon_count"] == 4
    )
