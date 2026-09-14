"""Missing-roster takeover commits one shared state and survives a restart."""

import json
import time
from types import SimpleNamespace

import pytest

from app.server.postgres_store import PostgreSQLIntelStore
from tests.test_missing_roster import presence, roster, primary
from tests.test_personnel_postgres import postgres_dsn  # noqa: F401


class CachedEnemy:
    def cached_name(self, name, *, allow_stale=False):
        return SimpleNamespace(name=name, category="character", entity_id=123), "cached"

    def cached_character_profile(self, character_id, *, allow_stale=False):
        return {"character_id": character_id, "name": "Enemy Pilot",
                "contact_standing": 0.0, "standing_source": "esi_contacts", "cache_status": "cached"}


@pytest.mark.parametrize("trigger", ["presence", "ocr"])
def test_takeover_commits_roster_event_projection_and_order(postgres_dsn, monkeypatch, trigger):  # noqa: F811
    clock = [time.time()]
    monkeypatch.setattr("app.server.capture_state.time.time", lambda: clock[0])
    store = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[], resolver=CachedEnemy())
    try:
        presence(store, "first", 1)
        presence(store, "good", 1)
        if trigger == "presence":
            roster(store, "good", 1)
        clock[0] += 16
        presence(store, "first", 2)
        presence(store, "first", 3)
        if trigger == "ocr":
            assert primary(store) == "first"
            roster(store, "good", 1)
        assert primary(store) == "good"
        last = store.list_intel_event_page()[-1]
        assert last["event_type"] == "alert.updated"
        assert last["payload"]["primary_client_id"] == "good"
        assert [p["name"] for p in last["payload"]["hostile_personnel"]] == ["Enemy Pilot"]
        items, _, version = store.read_active_event_snapshot()
        assert version == last["seq"]
        assert items[0]["metadata"]["primary_client_id"] == "good"
        with store._connect() as connection:
            stored = connection.execute("SELECT payload_json FROM system_current_state WHERE system_key = ?", ("s-kswl",)).fetchone()
            assert json.loads(stored["payload_json"]) == last["payload"]
    finally:
        store.close()
    reopened = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[])
    try:
        assert primary(reopened) == "good"
        assert reopened.list_intel_event_page()[-1] == last
    finally:
        reopened.close()


def test_failed_takeover_commit_does_not_publish_half_state(postgres_dsn, monkeypatch):  # noqa: F811
    clock = [time.time()]
    monkeypatch.setattr("app.server.capture_state.time.time", lambda: clock[0])
    store = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[], resolver=CachedEnemy())
    store._state_maintenance.close()
    try:
        presence(store, "first", 1)
        presence(store, "good", 1)
        roster(store, "good", 1)
        clock[0] += 16
        presence(store, "first", 2)
        previous = store.list_intel_event_page()
        persist = store._persist_intel_events

        def fail(connection, events):
            raise RuntimeError("takeover commit failed")

        monkeypatch.setattr(store, "_persist_intel_events", fail)
        with pytest.raises(RuntimeError, match="takeover commit failed"):
            presence(store, "first", 3)
        assert store.list_intel_event_page() == previous
        assert store.read_active_event_snapshot()[0][0]["metadata"]["primary_client_id"] == "first"
        monkeypatch.setattr(store, "_persist_intel_events", persist)
        assert store._state_repair.run() == 1
        last = store.list_intel_event_page()[-1]
        assert last["payload"]["primary_client_id"] == "good"
        assert last["payload"]["hostile_personnel"][0]["name"] == "Enemy Pilot"
        assert store.read_active_event_snapshot()[0][0]["metadata"]["primary_client_id"] == "good"
    finally:
        store.close()
