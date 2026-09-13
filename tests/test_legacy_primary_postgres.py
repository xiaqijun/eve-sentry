"""Legacy parent migration repairs durable canonical state, including on restart."""

from app.server.postgres_store import PostgreSQLIntelStore
from tests.test_authority_postgres import presence, store  # noqa: F401
from tests.test_personnel_postgres import postgres_dsn  # noqa: F401


def test_window_replaces_legacy_zero_in_events_and_snapshot(store):  # noqa: F811
    presence(store, "machine", 0, seen_at="2026-09-10T00:00:00+00:00")
    presence(store, "machine:user-one", 2)
    event = store.list_intel_event_page()[-1]
    assert event["event_type"] == "alert.entered"
    assert event["payload"]["primary_client_id"] == "machine:user-one"
    assert event["payload"]["hostile_count"] == 2
    assert (
        store.read_active_event_snapshot()[0][0]["metadata"]["hostile_icon_count"] == 2
    )
    store._stale_heartbeat_cleanup_after = 0
    store.expire_active_intel()
    parent = next(
        item
        for item in store._active_intel.values()
        if item.metadata.get("client_id") == "machine"
    )
    assert parent.metadata["left_reason"] == "target_removed"
    assert store.list_intel_event_page()[-1] == event
    presence(store, "machine:user-one", 0)
    assert store.list_intel_event_page()[-1]["event_type"] == "alert.cleared"
    assert store.read_active_event_snapshot()[0] == []


def test_restart_repairs_previously_suppressed_enemy_without_upload(
    store,  # noqa: F811
    postgres_dsn,  # noqa: F811
    monkeypatch,
):
    import app.server.source_authority as authority

    # Seed the same wrong canonical state the previous release produced.
    with monkeypatch.context() as old:
        old.setattr(authority, "superseded_parent_clients", lambda items: set())
        presence(store, "machine", 0, seen_at="2026-09-10T00:00:00+00:00")
        presence(store, "machine:user-one", 2)
        assert store.read_active_event_snapshot()[0] == []
        assert (
            store.list_intel_event_page()[-1]["payload"]["primary_client_id"]
            == "machine"
        )
        store.close()
    reopened = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[])
    try:
        event = reopened.list_intel_event_page()[-1]
        assert event["payload"]["primary_client_id"] == "machine:user-one"
        assert event["payload"]["hostile_count"] == 2
        assert (
            reopened.read_active_event_snapshot()[0][0]["metadata"][
                "hostile_icon_count"
            ]
            == 2
        )
        reopened._stale_heartbeat_cleanup_after = 0
        reopened.expire_active_intel()
        presence(reopened, "machine:user-one", 0, source_status="stopped")
        events = reopened.list_intel_event_page()
    finally:
        reopened.close()
    again = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[])
    try:
        assert (
            again.read_active_event_snapshot()[0][0]["metadata"]["freshness"]
            == "unknown"
        )
        assert again.list_intel_event_page() == events
        assert "s-kswl" not in authority.primary_sources(again._active_intel.values())
    finally:
        again.close()


def test_stop_then_restart_before_cleanup_does_not_resurrect_parent(
    store,  # noqa: F811
    postgres_dsn,  # noqa: F811
):
    from app.server.source_authority import primary_sources

    store._state_maintenance.close()
    presence(store, "machine", 0)
    presence(store, "machine:user-one", 1)
    presence(store, "machine:user-one", 0, source_status="stopped")
    before = store.list_intel_event_page()
    assert before[-1]["payload"]["freshness"] == "unknown"
    store.close()
    reopened = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[])
    try:
        assert "s-kswl" not in primary_sources(reopened._active_intel.values())
        assert reopened.list_intel_event_page() == before
        assert (
            reopened.read_active_event_snapshot()[0][0]["metadata"]["freshness"]
            == "unknown"
        )
    finally:
        reopened.close()
