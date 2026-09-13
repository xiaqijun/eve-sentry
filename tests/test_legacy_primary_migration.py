"""Retired machine-level Presence must not override per-window sources."""

from types import SimpleNamespace

from app.server.intel_store import IntelStore
from app.server.source_authority import primary_sources


def node(client, *, count=0, stopped=False, system="S-KSWL"):
    return SimpleNamespace(
        source="eve-sentry-detector",
        active=count > 0 and not stopped,
        system_name=system,
        first_seen_at="2026-09-10T00:00:00+00:00",
        metadata={
            "client_id": client,
            "presence_only": True,
            "hostile_icon_count": count,
            **({"left_reason": "monitor_stopped"} if stopped else {}),
        },
    )


def test_parent_zero_is_not_a_primary_once_window_sources_exist():
    old, current = node("machine"), node("machine:user-one", count=2)
    assert primary_sources([old, current])["s-kswl"] is current
    current.active = False
    current.metadata["left_reason"] = "monitor_stopped"
    assert primary_sources([old, current]) == {}


def test_migration_does_not_change_independent_machine_order_or_legacy_only():
    first, later = node("first"), node("other:user-one", count=2)
    assert primary_sources([first])["s-kswl"] is first
    assert primary_sources([first, later])["s-kswl"] is first
    # OCR query identities and similarly prefixed machine IDs are not windows.
    assert (
        primary_sources([first, node("first:query:q"), node("first-extra:user-one")])[
            "s-kswl"
        ]
        is first
    )


def test_migration_applies_across_systems_and_maintenance_persists_retirement(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    try:
        store.record_hostile_presence(
            {
                "client_id": "machine",
                "system_name": "S-KSWL",
                "hostile_icon_count": 0,
                "seen_at": "2026-09-10T00:00:00+00:00",
            }
        )
        store.record_hostile_presence(
            {
                "client_id": "machine:user-one",
                "system_name": "HB-FSO",
                "hostile_icon_count": 0,
            }
        )
        assert "s-kswl" not in primary_sources(store._active_intel.values())
        store._stale_heartbeat_cleanup_after = 0
        store.expire_active_intel()
        parent = next(
            item
            for item in store._active_intel.values()
            if item.metadata.get("client_id") == "machine"
        )
        assert parent.metadata["left_reason"] == "target_removed"
    finally:
        store.close()
