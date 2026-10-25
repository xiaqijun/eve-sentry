"""Pending relationship classification is distinct from a confirmed empty roster."""

import json
from types import SimpleNamespace

from app.core.active_intel import ActiveIntelItem
from app.intel.classification import ClassificationEngine
from app.intel.scoring import Watchlist
from app.server.http_server import IntelRequestHandler
from app.server.postgres_store import PostgreSQLIntelStore
from app.server.system_state import project_active_items


def pending_store():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._scorer = ClassificationEngine(watchlist=Watchlist())
    common = dict(source="eve-sentry-detector", source_instance="node", system_name="Tama")
    presence = ActiveIntelItem("presence", **common, target_type="system", metadata={
        "client_id": "node", "presence_only": True, "hostile_icon_count": 1,
        "source_join_order": 1,
    })
    pilot = ActiveIntelItem("pilot", **common, name="Pilot", character_id=1, metadata={
        "client_id": "node", "identity_status": "resolved", "character_profiles": [{
            "character_id": 1, "name": "Pilot", "corporation_id": 30,
            "standing_source": "esi_organization_pending", "affiliation_trusted": True,
        }],
    })
    store._active_intel = {"presence": presence, "pilot": pilot}
    return store


def test_pending_classification_marks_roster_incomplete_without_reusing_hostility():
    store = pending_store()
    state = store._hostile_system_state()["tama"]
    assert state["hostile_count"] == 1
    assert state["personnel"] == []
    assert state["personnel_complete"] is False
    store._active_intel["pilot"].metadata["character_profiles"][0].update(
        standing_source="esi_organization", contact_standing=5,
    )
    ready = store._hostile_system_state()["tama"]
    assert ready["personnel"] == []
    assert ready["personnel_complete"] is True
    events = store._hostile_state_events({"tama": state}, {"tama": ready}, "t1")
    assert len(events) == 1 and events[0]["payload"]["personnel_complete"] is True


def test_incomplete_empty_roster_survives_projection_and_bootstrap():
    store = pending_store()
    state = store._hostile_system_state()
    event = store._hostile_state_events({}, state, "t1")[0]
    items = project_active_items([], [{"system_key": "tama", "state_version": 1,
                                      "occurred_at": "t1", "payload_json": json.dumps(event["payload"])}])
    assert len(items) == 1
    assert items[0]["metadata"]["personnel_complete"] is False
    handler = IntelRequestHandler.__new__(IntelRequestHandler)
    handler.server = SimpleNamespace(store=store)
    rosters = handler._hostile_personnel_snapshot(items)
    assert len(rosters) == 1
    assert rosters[0]["hostile_count"] == 1
    assert rosters[0]["personnel"] == []
    assert rosters[0]["personnel_complete"] is False


def test_departed_pending_pilot_does_not_block_a_confirmed_empty_roster():
    store = pending_store()
    store._active_intel["pilot"].active = False
    assert store._hostile_system_state()["tama"]["personnel_complete"] is True
