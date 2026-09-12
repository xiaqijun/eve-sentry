"""First-resident authority, standby isolation and clear lifecycle regression."""

from app.server.intel_store import IntelStore
from app.server.postgres_store import PostgreSQLIntelStore
from app.server.source_authority import primary_sources


def presence(store, client, count, system="S-KSWL", **extra):
    return store.record_hostile_presence(
        {
            "client_id": client,
            "system_name": system,
            "hostile_icon_count": count,
            **extra,
        }
    )


def projected(store):
    reader = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    reader._active_intel = store._active_intel
    reader._scorer = store._scorer
    return reader._hostile_system_state()


def test_first_arrival_remains_primary_across_clear_and_standby_updates(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    presence(store, "first", 0)
    presence(store, "second", 7)
    assert projected(store) == {}
    first = primary_sources(store._active_intel.values())["s-kswl"]
    order = first.metadata["source_join_order"]
    presence(store, "first", 1)
    assert projected(store)["s-kswl"]["hostile_count"] == 1
    presence(store, "second", 9)
    assert projected(store)["s-kswl"]["primary_client_id"] == "first"
    presence(store, "first", 0)
    assert projected(store) == {}
    assert first.metadata["source_join_order"] == order
    store.close()


def test_zero_primary_leaves_system_and_returns_to_queue_tail(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    presence(store, "first", 0)
    presence(store, "second", 2)
    presence(store, "first", 0, "HB-FSO")
    assert projected(store)["s-kswl"]["primary_client_id"] == "second"
    presence(store, "first", 9)
    assert projected(store)["s-kswl"]["hostile_count"] == 2
    assert projected(store)["s-kswl"]["primary_client_id"] == "second"
    store.close()


def test_duplicate_or_out_of_order_presence_does_not_change_authority(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    presence(store, "first", 1, presence_version=10, presence_state_id="ten")
    presence(store, "second", 9, presence_version=100)
    assert presence(store, "first", 0, presence_version=9)["accepted"] is False
    assert presence(store, "first", 0, presence_version=10, presence_state_id="ten")[
        "duplicate"
    ]
    assert projected(store)["s-kswl"]["hostile_count"] == 1
    store.close()
