"""v1 consumers see the same committed roster and count as durable events."""

import json

from app.server.intel_store import IntelStore
from app.server.system_state import project_active_items


def state(count=1):
    return {
        "system_key": "s-kswl",
        "state_version": 10,
        "occurred_at": "2026-09-13T00:00:00+00:00",
        "payload_json": json.dumps(
            {
                "system_name": "S-KSWL",
                "hostile_count": count,
                "hostile_personnel": [{"character_id": 123, "name": "Pilot"}],
            }
        ),
    }


def test_projection_replaces_raw_roster_and_clear_suppresses_stale_rows():
    raw = [{"system_name": "S-KSWL", "name": "Old pilot"}, {"system_name": "Other"}]
    items = project_active_items(raw, [state()])
    assert not any(item.get("name") == "Old pilot" for item in items)
    assert [
        item["name"] for item in items if item.get("target_type") == "character"
    ] == ["Pilot"]
    assert project_active_items(raw, [state(0)]) == [{"system_name": "Other"}]


def test_map_uses_exact_projected_count_and_confirmed_roster():
    store = IntelStore.__new__(IntelStore)
    store._scorer = None
    data = store._aggregate_active_by_system(project_active_items([], [state(3)]))
    assert data["S-KSWL"]["hostile_count"] == 3
    assert data["S-KSWL"]["hostiles"] == ["Pilot"]
