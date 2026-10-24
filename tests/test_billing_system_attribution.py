"""Exercise name-only detector evidence through the production store path."""

from contextlib import contextmanager
import sqlite3

import pytest

from app.server.intel_store import IntelStore, StarSystem
from app.server.postgres_store import PostgreSQLIntelStore
from app.server.seat_billing import SeatBillingRepository, migrate_seat_billing_schema


@pytest.fixture
def billing_store(tmp_path):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    migrate_seat_billing_schema(connection)

    @contextmanager
    def connect():
        yield connection

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._load_reports = lambda: []
    IntelStore.__init__(store, tmp_path / "unused.json", systems={
        "R-YWID": StarSystem("R-YWID", 0, 0, system_id=30003631),
        "S-KSWL": StarSystem("S-KSWL", 1, 1, system_id=30003629),
    }, links=[])
    store._connect = connect
    store._upsert_active_intel_rows = lambda _connection, _rows: None
    store._persist_hostile_wave_changes = lambda _connection, _changes: None
    store._persist_intel_events = lambda _connection, _events: None
    try:
        yield store, SeatBillingRepository(connect)
    finally:
        IntelStore.close(store)
        connection.close()


def record_presence(store, system_name, **extra):
    for seen_at in ("2026-10-24T14:00:00+00:00", "2026-10-24T14:00:10+00:00"):
        store.record_hostile_presence({
            "client_id": "detector-" + system_name,
            "system_name": system_name,
            "hostile_icon_count": 0,
            "seen_at": seen_at,
            "server_received_at": seen_at,
            "seat_account_id": "account-a",
            "seat_key_id": "detector-key",
            **extra,
        })


def test_name_only_presence_produces_two_attributed_usage_intervals(billing_store):
    store, repository = billing_store
    record_presence(store, "R-YWID")
    record_presence(store, "s-kswl")
    contributions = repository.list_monitor_contributions()["contributions"]
    assert {row["system_id"] for row in contributions} == {30003631, 30003629}
    result = repository.record_client_usage({
        "account_id": "account-a", "key_id": "alert-key", "client_id": "alert-client",
        "system_ids": [30003631, 30003629],
        "started_at": "2026-10-24T14:00:00+00:00",
        "ended_at": "2026-10-24T14:00:10+00:00", "duration_seconds": 10,
    })
    assert {row["system_id"] for row in result["usages"]} == {"30003631", "30003629"}
    assert [row["duration_seconds"] for row in result["usages"]] == [10, 10]


def test_unknown_system_is_not_guessed(billing_store):
    store, repository = billing_store
    record_presence(store, "unknown-system")
    row, = repository.list_monitor_contributions()["contributions"]
    assert row["system_id"] is None


def test_explicit_system_id_remains_compatible(billing_store):
    store, repository = billing_store
    record_presence(store, "outside-map", system_id=30000142)
    row, = repository.list_monitor_contributions()["contributions"]
    assert row["system_id"] == 30000142
