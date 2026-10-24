"""Selected warning centers must bound authenticated online-time charges."""

import sqlite3

import pytest

from app.server.intel_store import IntelStore, StarSystem
from app.server.postgres_store import PostgreSQLIntelStore
from app.server.seat_billing import SeatBillingRepository, migrate_seat_billing_schema


@pytest.fixture
def scoped_store(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    migrate_seat_billing_schema(connection)
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._load_reports = lambda: []
    IntelStore.__init__(store, tmp_path / "unused.json", systems={
        "R-YWID": StarSystem("R-YWID", 0, 0, system_id=30003631),
        "S-KSWL": StarSystem("S-KSWL", 1, 1, system_id=30003629),
    }, links=[])
    store._connect = lambda: connection
    store._write_heartbeat = lambda _raw: None
    repository = SeatBillingRepository(store._connect)
    for sid, name in ((30003631, "R-YWID"), (30003629, "S-KSWL")):
        repository.record_monitor_contribution({
            "account_id": "account-a", "key_id": "monitor-key",
            "client_id": "monitor-" + name, "system_id": sid, "system_name": name,
            "primary_generation": 1, "started_at": "2026-10-25T00:00:00+00:00",
            "ended_at": "2026-10-25T00:01:00+00:00", "duration_seconds": 60,
        })
    try:
        yield store, repository
    finally:
        IntelStore.close(store)
        connection.close()


def heartbeat(store, second, scope=None, connection="connection-a"):
    details = {"connection_id": connection}
    if scope is not None:
        details["alert_scope"] = scope
    return store.record_heartbeat({
        "client_id": "one-alert-client", "client_type": "alert_client",
        "seat_account_id": "account-a", "seat_key_id": "alert-key",
        "seen_at": f"2026-10-25T00:00:{second:02d}+00:00",
        "heartbeat_interval_seconds": 10, "details": details,
    })


def selection(*names, revision="selection-a"):
    return {"version": 1, "revision": revision, "systems": [{"name": name} for name in names]}


def test_one_selected_center_does_not_charge_other_monitoring_machine(scoped_store):
    store, repository = scoped_store
    scope = selection("R-YWID")
    heartbeat(store, 0, scope)
    heartbeat(store, 10, scope)
    heartbeat(store, 10, scope)  # replay cannot debit again
    rows = repository.list_client_usage()["usage"]
    assert [(row["system_id"], row["duration_seconds"]) for row in rows] == [("30003631", 10)]


@pytest.mark.parametrize("scope", [
    None, {}, selection(), selection("Unknown"), selection("R-YWID", "Unknown"),
    {"version": True, "revision": "r", "systems": [{"name": "R-YWID"}]},
    {"version": 1, "revision": "", "systems": [{"name": "R-YWID"}]},
    {"version": 1, "revision": "r", "systems": [{"name": "R-YWID", "id": 30003629}]},
])
def test_missing_empty_or_unresolvable_scope_never_falls_back_to_monitors(scoped_store, scope):
    store, repository = scoped_store
    heartbeat(store, 0, scope)
    heartbeat(store, 10, scope)
    assert repository.list_client_usage()["usage"] == []


@pytest.mark.parametrize("entry,expected", [
    ({"id": 30003631}, ["30003631"]),
    ({"id": "30003631", "name": "r-ywid"}, ["30003631"]),
    ({"id": True}, []),
    ({"id": 30000000}, []),
    ({"id": "9" * 5000}, []),
    ({"name": None}, []),
], ids=["id-only", "matching-name", "boolean-id", "unknown-id", "oversized-id", "invalid-name"])
def test_scope_ids_are_validated_against_server_map(scoped_store, entry, expected):
    store, repository = scoped_store
    scope = {"version": 1, "revision": "selection-a", "systems": [entry]}
    heartbeat(store, 0, scope)
    heartbeat(store, 10, scope)
    assert [row["system_id"] for row in repository.list_client_usage()["usage"]] == expected


def test_scope_changes_skip_boundary_then_charge_only_new_center(scoped_store):
    store, repository = scoped_store
    heartbeat(store, 0, selection("R-YWID"))
    heartbeat(store, 10, selection("R-YWID"))
    heartbeat(store, 20, selection("S-KSWL", revision="selection-b"))
    heartbeat(store, 30, selection("S-KSWL", revision="selection-b"))
    rows = repository.list_client_usage()["usage"]
    assert sorted((row["system_id"], row["started_at"]) for row in rows) == [
        ("30003629", "2026-10-25T00:00:20+00:00"),
        ("30003631", "2026-10-25T00:00:00+00:00"),
    ]


@pytest.mark.parametrize("change", ["revision", "connection", "systems"])
def test_reconnect_or_scope_change_never_charges_across_boundary(scoped_store, change):
    store, repository = scoped_store
    heartbeat(store, 0, selection("R-YWID"))
    scope = selection("R-YWID", revision="selection-b") if change == "revision" else selection("R-YWID")
    if change == "systems":
        scope = selection("S-KSWL")  # even a reused revision cannot expand scope
    heartbeat(store, 10, scope, connection="connection-b" if change == "connection" else "connection-a")
    assert repository.list_client_usage()["usage"] == []


def test_selected_centers_are_deduplicated(scoped_store):
    store, repository = scoped_store
    scope = selection("R-YWID", "r-ywid", "S-KSWL")
    heartbeat(store, 0, scope)
    heartbeat(store, 10, scope)
    rows = repository.list_client_usage()["usage"]
    assert sorted((row["system_id"], row["duration_seconds"]) for row in rows) == [("30003629", 10), ("30003631", 10)]


@pytest.mark.parametrize("fields", [{}, {"system_ids": []}, {"system_id": ""}])
def test_repository_requires_explicit_systems(scoped_store, fields):
    _store, repository = scoped_store
    result = repository.record_client_usage({
        "account_id": "account-a", "key_id": "alert-key", "client_id": "one-alert-client",
        "started_at": "2026-10-25T00:00:00+00:00", "ended_at": "2026-10-25T00:00:10+00:00",
        "duration_seconds": 10, **fields,
    })
    assert result["created"] is False
    assert result["usages"] == []
    assert repository.list_client_usage()["usage"] == []
