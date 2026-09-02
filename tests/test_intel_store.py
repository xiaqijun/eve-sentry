import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.active_intel import ActiveIntelItem
from app.core.models import Evidence, Observation, ThreatEvent
from app.esi.cache import EsiCache
from app.esi.resolver import EsiResolver
from app.intel.enrichment import ThreatEnricher, ThreatEnrichment
from app.intel.scoring import ScoringEngine, Watchlist
from app.server.intel_store import (
    HeartbeatOwnershipConflict,
    IntelReport,
    IntelStore,
    StarSystem,
)


def test_record_ocr_snapshot_resolves_every_full_roster_name_through_esi(tmp_path):
    resolved_names = []

    class Resolver:
        def enrich_observation(self, observation):
            name = observation.names[0]
            resolved_names.append(name)
            character_id = {
                "Friendly Pilot": 101,
                "Hostile Pilot": 102,
            }.get(name)
            observation.character_ids = [character_id] if character_id else []
            return observation

        def character_profile(self, character_id):
            return {
                "character_id": int(character_id),
                "name": "Friendly Pilot" if character_id == 101 else "Hostile Pilot",
            }

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=Resolver(),
    )
    try:
        result = store.record_ocr_snapshot(
            {
                "client_id": "detector-client:test",
                "source_instance": "EVE - Pilot",
                "system_name": "S-KSWL",
                "hostile_icon_count": 2,
                "names": ["Friendly Pilot", "Hostile Pilot", "Unreadable Pilot"],
            }
        )
        assert store.wait_for_esi_idle(timeout=1.0)
    finally:
        store.close()

    assert result["created"] == 3
    assert set(resolved_names) == {
        "Friendly Pilot",
        "Hostile Pilot",
        "Unreadable Pilot",
    }


def test_async_ocr_identity_resolution_notifies_state_consumers(tmp_path):
    class Resolver:
        def enrich_observation(self, observation):
            observation.character_ids = [123]
            return observation

    notifications = []
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=Resolver(),
    )
    store.set_change_notifier(lambda: notifications.append(True))
    try:
        store.record_ocr_snapshot(
            {
                "client_id": "detector-client:test",
                "system_name": "S-KSWL",
                "names": ["Pilot"],
                "hostile_icon_count": 1,
            }
        )
        assert store.wait_for_esi_idle(timeout=1.0)
    finally:
        store.close()

    assert notifications == [True]


def test_ocr_esi_refresh_invalidates_character_profile_layers(tmp_path):
    cache = EsiCache(tmp_path / "esi_cache.json")
    cache.set(
        "character:2115746410",
        {
            "character_id": 2115746410,
            "corporation_id": 1000168,
            "alliance_id": None,
        },
        ttl_seconds=3600,
    )
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=SimpleNamespace(cache=cache),
    )
    store._character_profile_cache[2115746410] = {
        "character_id": 2115746410,
        "corporation_id": 1000168,
    }
    observation = Observation(
        source="eve-sentry-detector",
        system_name="HB-FSO",
        names=["Samcat"],
        character_ids=[2115746410],
        raw_text="Samcat",
        seen_at="2026-09-01T13:48:02+00:00",
        received_at="2026-09-01T13:48:03+00:00",
        observation_id="obs-samcat-cache-refresh",
    )

    try:
        store._invalidate_ocr_character_profile_cache(observation)

        assert 2115746410 not in store._character_profile_cache
        assert cache.get("character:2115746410") is None
    finally:
        store.close()


def test_resume_pending_ocr_identity_tasks_after_restart(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    observation = store.add_observation(
        {
            "source": "eve-sentry-detector",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Pending Pilot"],
        }
    )
    active_id = "ocr:pending"
    store._active_intel[active_id] = ActiveIntelItem(
        active_id=active_id,
        source="eve-sentry-detector",
        source_instance="EVE - Pilot",
        system_name="S-KSWL",
        target_type="character",
        name="Pending Pilot",
        metadata={
            "client_id": "detector-client:test",
            "identity_status": "pending",
        },
        active=True,
        source_observation_ids=[observation.observation_id],
    )
    submitted = []
    store._resolver = object()
    store._esi_worker = SimpleNamespace(
        submit=lambda key, task: submitted.append((key, task)) or True,
    )

    assert store._resume_pending_ocr_esi_tasks() == 1
    assert len(submitted) == 1
    key, task = submitted[0]
    assert key == active_id
    assert task.active_id == active_id
    assert task.report_id == observation.observation_id
    assert task.client_id == "detector-client:test"
    assert task.original_name == "Pending Pilot"


def test_list_alerts_stops_scoring_after_reaching_limit(tmp_path, monkeypatch):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    observations = [
        store.add_observation(
            {
                "source": "manual",
                "system_name": "Tama",
                "names": [f"Pilot {index}"],
                "seen_at": f"2026-07-30T12:00:0{index}+00:00",
                "received_at": f"2026-07-30T12:00:0{index}+00:00",
            }
        )
        for index in range(1, 4)
    ]
    scored = []
    original = store._alert_from_report

    def recording_alert(report):
        scored.append(report.report_id)
        return original(report)

    monkeypatch.setattr(store, "_alert_from_report", recording_alert)

    alerts = store.list_alerts(limit=1)

    assert [item["source_observation_id"] for item in alerts] == [
        observations[-1].observation_id
    ]
    assert scored == [observations[-1].observation_id]


def test_alert_for_observation_scores_only_requested_report(tmp_path, monkeypatch):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    observations = [
        store.add_observation(
            {
                "source": "manual",
                "system_name": "Tama",
                "names": [f"Pilot {index}"],
            }
        )
        for index in range(3)
    ]
    scored = []
    original = store._alert_from_report

    def recording_alert(report):
        scored.append(report.report_id)
        return original(report)

    monkeypatch.setattr(store, "_alert_from_report", recording_alert)

    alert = store.alert_for_observation(observations[1].observation_id)

    assert alert["source_observation_id"] == observations[1].observation_id
    assert scored == [observations[1].observation_id]


def test_heartbeat_summary_tracks_types_statuses_and_stale_clients(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    now = datetime.now(timezone.utc)

    store.record_heartbeat(
        {
            "client_id": "detector:test",
            "client_type": "detector_client",
            "label": "Detector Client",
            "status": "running",
            "heartbeat_interval_seconds": 5,
            "seen_at": (now - timedelta(seconds=2)).isoformat(),
            "details": {"system": "Tama"},
        }
    )
    store.record_heartbeat(
        {
            "client_id": "alert:test",
            "client_type": "alert_client",
            "label": "Alert Client",
            "status": "idle",
            "heartbeat_interval_seconds": 5,
            "seen_at": (now - timedelta(seconds=45)).isoformat(),
            "details": {"transport": "poll"},
        }
    )

    payload = store.heartbeat_snapshot()
    summary = payload["summary"]

    assert payload["count"] == 2
    assert len(payload["heartbeats"]) == 2
    assert summary["count"] == 2
    assert summary["online_count"] == 1
    assert summary["stale_count"] == 1
    assert summary["by_type"] == {
        "detector_client": 1,
        "alert_client": 1,
    }
    assert summary["by_status"] == {
        "running": 1,
        "idle": 1,
    }
    assert summary["latest_seen_at"] == payload["heartbeats"][0]["seen_at"]


def test_heartbeat_management_attribution_stays_out_of_public_snapshots(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])

    response = store.record_heartbeat(
        {
            "client_id": "detector:test",
            "client_type": "detector_client",
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "203.0.113.9",
        }
    )
    store.record_heartbeat(
        {
            "client_id": "legacy:test",
            "client_type": "alert_client",
        }
    )

    assert "user_id" not in response
    public = store.heartbeat_snapshot()
    assert all("user_id" not in item for item in public["heartbeats"])
    assert all("api_key_id" not in item for item in public["summary"]["items"])
    assert all("remote_ip" not in item for item in public["heartbeats"])

    managed = {
        item["client_id"]: item
        for item in store.management_heartbeat_snapshot()["heartbeats"]
    }
    assert managed["detector:test"]["user_id"] == "user-1"
    assert managed["detector:test"]["api_key_id"] == "key-1"
    assert managed["detector:test"]["remote_ip"] == "203.0.113.9"
    assert managed["legacy:test"]["user_id"] == ""
    assert managed["legacy:test"]["api_key_id"] == ""
    assert managed["legacy:test"]["remote_ip"] == ""


def test_heartbeat_client_id_cannot_move_between_users(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    client_id = "detector:shared"

    store.record_heartbeat(
        {
            "client_id": client_id,
            "user_id": "user-1",
            "api_key_id": "key-1",
        }
    )

    with pytest.raises(HeartbeatOwnershipConflict):
        store.record_heartbeat(
            {
                "client_id": client_id,
                "user_id": "user-2",
                "api_key_id": "key-2",
            }
        )

    with pytest.raises(HeartbeatOwnershipConflict):
        store.record_heartbeat({"client_id": client_id})

    store.record_heartbeat(
        {
            "client_id": client_id,
            "user_id": "user-1",
            "api_key_id": "key-3",
        }
    )
    managed = store.management_heartbeat_snapshot()["heartbeats"][0]
    assert managed["user_id"] == "user-1"
    assert managed["api_key_id"] == "key-3"


@pytest.mark.parametrize(
    "upload_client_id",
    ["detector:parent", "detector:window-a"],
)
def test_refresh_detector_heartbeat_matches_parent_without_changing_details(
    tmp_path,
    monkeypatch,
    upload_client_id,
):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    original_details = {
        "monitoring": True,
        "system_name": "S-KSWL",
        "targets": [
            {
                "client_id": "detector:window-a",
                "system_name": "S-KSWL",
                "monitoring": True,
            },
            {
                "client_id": "detector:window-b",
                "system_name": "R-YWID",
                "monitoring": False,
            },
        ],
    }
    store.record_heartbeat(
        {
            "client_id": "detector:parent",
            "client_type": "detector_client",
            "label": "Detector",
            "status": "running",
            "seen_at": "2026-08-28T00:00:00+00:00",
            "heartbeat_interval_seconds": 15,
            "details": original_details,
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "203.0.113.9",
        }
    )
    monkeypatch.setattr(
        "app.server.intel_store.utc_now_iso",
        lambda: "2026-08-28T00:00:30+00:00",
    )

    refreshed = store.refresh_detector_heartbeat(upload_client_id)

    assert refreshed is not None
    assert refreshed["client_id"] == "detector:parent"
    assert refreshed["seen_at"] == "2026-08-28T00:00:30+00:00"
    managed = store.management_heartbeat_snapshot()["heartbeats"][0]
    assert managed["status"] == "running"
    assert managed["heartbeat_interval_seconds"] == 15
    assert managed["details"] == original_details
    assert managed["user_id"] == "user-1"
    assert managed["api_key_id"] == "key-1"
    assert managed["remote_ip"] == "203.0.113.9"
    assert store.refresh_detector_heartbeat("detector:missing") is None


def test_add_report_persists_and_snapshot_aggregates(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(
        path,
        systems={"Tama": StarSystem("Tama", 10, 20, "The Citadel", 0.3)},
        links=[],
    )

    report = store.add_report(
        system=" Tama ",
        names=[" Alice ", "Bob", "Alice"],
        source="test",
        seen_at="2026-06-29T12:00:00+00:00",
    )

    assert report.system == "Tama"
    assert report.names == ["Alice", "Bob"]

    reloaded = IntelStore(path, systems={}, links=[])
    snapshot = reloaded.snapshot()

    assert snapshot["summary"]["report_count"] == 1
    assert snapshot["summary"]["observation_count"] == 1
    assert snapshot["summary"]["alert_count"] == 0
    assert snapshot["summary"]["hostile_count"] == 2
    assert snapshot["systems"][0]["name"] == "Tama"
    assert snapshot["systems"][0]["hostiles"] == []
    assert snapshot["systems"][0]["hostile_count"] == 0
    assert snapshot["systems"][0]["report_count"] == 0
    assert snapshot["observations"][0]["system_name"] == "Tama"
    assert snapshot["alerts"] == []
    assert reloaded.list_alerts()[0]["level"] == "low"


def test_legacy_json_reports_gain_stable_stream_positions_on_next_save(tmp_path):
    path = tmp_path / "intel_reports.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-1",
                    "system": "Tama",
                    "names": ["Pilot 1"],
                    "source": "manual",
                    "seen_at": "2026-08-01T10:00:00+00:00",
                    "received_at": "2026-08-01T10:00:00+00:00",
                },
                {
                    "id": "legacy-2",
                    "system": "Tama",
                    "names": ["Pilot 2"],
                    "source": "manual",
                    "seen_at": "2026-08-01T10:00:00+00:00",
                    "received_at": "2026-08-01T10:00:00+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )
    store = IntelStore(path, systems={}, links=[])
    first_cursor = store.resolve_alert_stream_cursor("evt_legacy-1")
    second_cursor = store.resolve_alert_stream_cursor("evt_legacy-2")

    store.add_report("Tama", ["Pilot 3"])
    persisted = json.loads(path.read_text(encoding="utf-8"))
    reloaded = IntelStore(path, systems={}, links=[])

    assert first_cursor is not None and second_cursor is not None
    assert first_cursor < second_cursor
    assert all(item["_stream_position"] > 0 for item in persisted)
    assert len({item["_stream_position"] for item in persisted}) == 3
    assert reloaded.resolve_alert_stream_cursor("evt_legacy-1") == first_cursor
    assert reloaded.resolve_alert_stream_cursor("evt_legacy-2") == second_cursor
    assert "_stream_position" not in reloaded.list_reports()[0]["metadata"]


def test_json_stream_positions_follow_persistence_order(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    constructed_first = IntelReport(system="Tama", names=["First"])
    persisted_first = IntelReport(system="Tama", names=["Second"])

    assert constructed_first.stream_position == 0
    assert persisted_first.stream_position == 0
    with store._lock:
        store._reports.append(persisted_first)
        store._save_reports()
        store._reports.append(constructed_first)
        store._save_reports()

    assert persisted_first.stream_position < constructed_first.stream_position
    assert store._report_stream_cursor(persisted_first) < (
        store._report_stream_cursor(constructed_first)
    )


def test_snapshot_map_uses_only_active_intel_for_system_hotness(tmp_path):
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={"Tama": StarSystem("Tama", 10, 20, "The Citadel", 0.3)},
        links=[],
    )

    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": "Tama",
            "names": ["Alice"],
            "raw_text": "Tama Alice",
            "metadata": {"hostile_count": 1, "sender": "Scout A"},
            "seen_at": "2000-01-01T00:00:00+00:00",
        }
    )

    snapshot = store.snapshot()

    assert snapshot["summary"]["report_count"] == 1
    assert snapshot["summary"]["hostile_count"] == 1
    assert snapshot["summary"]["active_system_count"] == 0
    assert snapshot["systems"][0]["name"] == "Tama"
    assert snapshot["systems"][0]["hostiles"] == []
    assert snapshot["systems"][0]["hostile_count"] == 0
    assert snapshot["systems"][0]["latest_seen"] is None
    assert snapshot["systems"][0]["report_count"] == 0


def test_snapshot_map_excludes_friendly_ocr_active_intel_from_hostile_counts(tmp_path):
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={"S-KSWL": StarSystem("S-KSWL", 10, 20, "Tenal", -0.3)},
        links=[],
    )
    store._active_intel["friendly"] = ActiveIntelItem(
        active_id="friendly",
        source="eve-sentry-detector",
        source_instance="EVE - Hajimi6",
        system_name="S-KSWL",
        system_id=30003629,
        character_id=2124219939,
        target_type="character",
        name="Hajimi6",
        raw_text="Hajimi6",
        metadata={
            "client_id": "detector-client:test",
            "contact_standing": 10.0,
            "standing_source": "esi_self",
            "standing_contact_type": "character",
        },
        first_seen_at="2026-07-10T01:00:00+00:00",
        last_seen_at="2026-07-10T01:00:00+00:00",
        active=True,
        seen_count=1,
        source_observation_ids=[],
    )

    snapshot = store.snapshot()

    assert snapshot["summary"]["hostile_count"] == 0
    assert snapshot["summary"]["active_system_count"] == 0
    assert snapshot["systems"][0]["hostiles"] == []
    assert snapshot["systems"][0]["hostile_count"] == 0
    assert snapshot["systems"][0]["report_count"] == 0


def test_active_aggregation_excludes_friendly_ocr_from_detector_visual_counts(tmp_path):
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={"S-KSWL": StarSystem("S-KSWL", 10, 20, "Tenal", -0.3)},
        links=[],
    )

    active_items = [
        {
            "source": "eve-sentry-detector",
            "target_type": "system",
            "system_name": "S-KSWL",
            "metadata": {
                "presence_only": True,
                "hostile_icon_count": 2,
                "hostile_icon_seen_at": "2026-07-10T01:00:00+00:00",
            },
            "last_seen_at": "2026-07-10T01:00:00+00:00",
        },
        {
            "source": "eve-sentry-detector",
            "target_type": "character",
            "system_name": "S-KSWL",
            "name": "Friendly Pilot",
            "metadata": {
                "hostile_icon_count": 2,
                "identity_status": "resolved",
                "contact_standing": 10.0,
            },
            "last_seen_at": "2026-07-10T01:00:00+00:00",
        },
    ]

    snapshot = store._aggregate_active_by_system(active_items)

    assert snapshot["S-KSWL"]["hostile_count"] == 2


def test_configured_map_does_not_add_unmapped_report_system_to_snapshot(tmp_path):
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={
            "0-UVHJ": StarSystem("0-UVHJ", 100, 120, "Tenal", -0.1),
        },
        links=[],
        allow_unmapped_systems=False,
    )

    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": "Jita",
            "names": ["Alice"],
            "raw_text": "Jita Alice",
            "metadata": {"hostile_count": 1, "sender": "Scout A"},
            "seen_at": "2099-01-01T00:00:00+00:00",
        }
    )

    snapshot = store.snapshot()

    assert [system["name"] for system in snapshot["systems"]] == ["0-UVHJ"]
    assert snapshot["reports"][0]["system_name"] == "Jita"
    assert snapshot["summary"]["system_count"] == 1
    assert snapshot["summary"]["active_system_count"] == 0
    assert store.list_active_intel()[0]["system_name"] == "Jita"


def test_list_reports_filters_and_limits(tmp_path):
    store = IntelStore(tmp_path / "intel_reports.json", systems={}, links=[])
    store.add_report("Tama", ["Alice"], seen_at="2026-06-29T12:00:00+00:00")
    store.add_report("Jita", ["Bob"], seen_at="2026-06-29T12:01:00+00:00")
    store.add_report("Tama", ["Carol"], seen_at="2026-06-29T12:02:00+00:00")

    assert [r["names"][0] for r in store.list_reports(limit=2)] == ["Carol", "Bob"]
    assert [r["names"][0] for r in store.list_reports(system="tama")] == [
        "Carol",
        "Alice",
    ]
    assert [r["system"] for r in store.list_reports(name="bob")] == ["Jita"]


def test_delete_report_removes_and_persists(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])
    report = store.add_report("Tama", ["Alice"])

    assert store.delete_report(report.report_id) is True
    assert store.delete_report(report.report_id) is False
    assert IntelStore(path, systems={}, links=[]).snapshot()["summary"][
        "report_count"
    ] == 0


def test_add_observation_persists_and_lists_alerts(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": " Tama ",
            "names": [" Alice ", "Alice"],
            "raw_text": "Tama Alice",
            "metadata": {"hostile_count": 1, "sender": "Scout A"},
            "seen_at": "2026-06-29T12:00:00+00:00",
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    assert observation.system_name == "Tama"
    assert observation.names == ["Alice"]

    reloaded = IntelStore(path, systems={}, links=[])
    observations = reloaded.list_observations(source="intel_channel")
    alerts = reloaded.list_alerts()

    assert observations[0]["raw_text"] == "Tama Alice"
    assert observations[0]["metadata"] == {
        "hostile_count": 1,
        "sender": "Scout A",
    }
    assert alerts[0]["id"] == f"evt_{observation.observation_id}"
    assert alerts[0]["score"] == 30
    assert alerts[0]["evidence"][0]["type"] == "intel_channel_observed"
    assert alerts[0]["acknowledged"] is False


def test_record_ocr_snapshot_creates_and_refreshes_active_intel(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    first = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice", "Bob"],
        }
    )
    second = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:02+00:00",
            "names": ["Alice", "Bob"],
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")

    assert first["created"] == 2
    assert second["refreshed"] == 2
    assert len(active) == 2
    assert {item["name"] for item in active} == {"Alice", "Bob"}
    assert all(item["seen_count"] == 1 for item in active)
    assert len(store.list_observations()) == 2


def test_record_ocr_snapshot_keeps_ended_wave_when_name_reenters(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot({**payload, "seen_at": "2026-07-03T10:00:00+00:00"})
    store.record_ocr_snapshot({**payload, "seen_at": "2026-07-03T10:00:01+00:00"})
    store.record_ocr_snapshot(
        {**payload, "names": [], "seen_at": "2026-07-03T10:00:20+00:00"}
    )
    store.record_ocr_snapshot(
        {**payload, "names": [], "seen_at": "2026-07-03T10:00:40+00:00"}
    )
    store.record_ocr_snapshot(
        {**payload, "names": [], "seen_at": "2026-07-03T10:01:00+00:00"}
    )
    store.record_ocr_snapshot({**payload, "seen_at": "2026-07-03T10:01:20+00:00"})

    rows = store.list_active_intel(source="eve-sentry-detector", active=True)
    assert len(rows) == 1
    assert rows[0]["active"] is True
    assert len(store.list_active_intel(source="eve-sentry-detector", active=False)) == 1


def test_record_ocr_snapshot_uses_complete_roster_not_coordinate_evidence(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "hostile_icon_count": 1,
            "names": ["Friendly Pilot", "Enemy Pilot"],
            "ocr_candidates": [
                {"text": "Friendly Pilot", "left": 20, "top": 4, "right": 100, "bottom": 14},
                {"text": "STARKEY", "left": 20, "top": 20, "right": 80, "bottom": 30},
                {"text": "07", "left": 84, "top": 20, "right": 100, "bottom": 30},
            ],
            "hostile_icons": [
                {"left": 6, "top": 20, "right": 16, "bottom": 30},
            ],
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")
    assert {item["name"] for item in active} == {"Friendly Pilot", "Enemy Pilot"}


def test_record_ocr_snapshot_does_not_add_coordinate_only_names(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "hostile_icon_count": 1,
            "names": ["784 fvtr", "Hajimi6", "Jone Rayl", "Misriah 2442"],
            "ocr_candidates": [
                {"text": "784 fvtr", "left": 20, "top": 31, "right": 74, "bottom": 46},
                {"text": "Hajimi6", "left": 19, "top": 61, "right": 72, "bottom": 79},
                {"text": "= Jone Rayl", "left": 8, "top": 90, "right": 85, "bottom": 105},
                {"text": "Misriah 2442", "left": 9, "top": 105, "right": 107, "bottom": 119},
            ],
            "hostile_icons": [
                {"left": 8, "top": 92, "right": 19, "bottom": 103},
            ],
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")
    assert {item["name"] for item in active} == {
        "784 fvtr",
        "Hajimi6",
        "Jone Rayl",
        "Misriah 2442",
    }


def test_detector_hostility_uses_resolved_identity_instead_of_aggregate_icon_count(
    tmp_path,
):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
    )

    base = {
        "active": True,
        "source": "eve-sentry-detector",
        "target_type": "character",
        "system_name": "S-KSWL",
        "metadata": {
            "hostile_icon_count": 4,
            "identity_status": "resolved",
        },
    }

    assert not store._active_item_is_hostile(
        {
            **base,
            "name": "Friendly Pilot",
            "character_id": 101,
            "metadata": {**base["metadata"], "contact_standing": 10.0},
        }
    )
    assert store._active_item_is_hostile(
        {
            **base,
            "name": "Hostile Pilot",
            "character_id": 102,
            "metadata": {**base["metadata"], "contact_standing": -5.0},
        }
    )
    assert store._active_item_is_hostile(
        {
            **base,
            "name": "Neutral Pilot",
            "character_id": 103,
            "metadata": {**base["metadata"], "contact_standing": 0.0},
        }
    )
    assert not store._active_item_is_hostile(
        {
            **base,
            "name": "Unresolved OCR Text",
            "character_id": None,
            "metadata": {
                "hostile_icon_count": 4,
                "identity_status": "unresolved",
            },
        }
    )
    assert store._active_item_is_hostile(
        {
            **base,
            "target_type": "system",
            "name": "",
            "character_id": None,
            "metadata": {
                "hostile_icon_count": 4,
                "presence_only": True,
            },
        }
    )


def test_detector_hostility_uses_esi_hostile_affiliation_without_standing(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(
                hostile_corporation_ids={42},
                hostile_alliance_ids={77},
            ),
            cooldown_seconds=0,
        ),
    )

    base = {
        "source": "eve-sentry-detector",
        "target_type": "character",
        "system_name": "S-KSWL",
        "character_id": 123,
        "metadata": {
            "hostile_icon_count": 1,
            "identity_status": "resolved",
        },
    }

    assert store._active_item_is_hostile(
        {
            **base,
            "name": "Hostile Corp Pilot",
            "metadata": {
                **base["metadata"],
                "character_profiles": [{"character_id": 123, "corporation_id": 42}],
            },
        }
    )
    assert store._active_item_is_hostile(
        {
            **base,
            "name": "Hostile Alliance Pilot",
            "metadata": {
                **base["metadata"],
                "character_profiles": [{"character_id": 123, "alliance_id": 77}],
            },
        }
    )


def test_detector_hostility_allows_friendly_alliance_with_neutral_standing(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(friendly_alliance_ids={99003581}),
            cooldown_seconds=0,
        ),
    )

    assert not store._active_item_is_hostile(
        {
            "source": "eve-sentry-detector",
            "target_type": "character",
            "system_name": "HB-FSO",
            "name": "Samcat",
            "character_id": 2115746410,
            "metadata": {
                "hostile_icon_count": 2,
                "identity_status": "resolved",
                "character_profiles": [
                    {
                        "character_id": 2115746410,
                        "corporation_id": 98524084,
                        "alliance_id": 99003581,
                        "contact_standing": 0.0,
                    }
                ],
            },
        }
    )


def test_detector_hostility_keeps_non_neutral_threshold_match_hostile(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(
                friendly_alliance_ids={99003581},
                hostile_standing_threshold=3.0,
            ),
            cooldown_seconds=0,
        ),
    )

    assert store._active_item_is_hostile(
        {
            "source": "eve-sentry-detector",
            "target_type": "character",
            "system_name": "HB-FSO",
            "name": "Samcat",
            "character_id": 2115746410,
            "metadata": {
                "hostile_icon_count": 2,
                "identity_status": "resolved",
                "character_profiles": [
                    {
                        "character_id": 2115746410,
                        "corporation_id": 98524084,
                        "alliance_id": 99003581,
                        "contact_standing": 2.0,
                    }
                ],
            },
        }
    )


def test_detector_hostility_does_not_hide_another_neutral_profile(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(friendly_alliance_ids={99003581}),
            cooldown_seconds=0,
        ),
    )

    assert store._active_item_is_hostile(
        {
            "source": "eve-sentry-detector",
            "target_type": "character",
            "system_name": "HB-FSO",
            "name": "Samcat",
            "character_id": 2115746410,
            "metadata": {
                "hostile_icon_count": 2,
                "identity_status": "resolved",
                "character_profiles": [
                    {
                        "character_id": 2115746410,
                        "alliance_id": 99003581,
                        "contact_standing": 0.0,
                    },
                    {"character_id": 123, "contact_standing": 0.0},
                ],
            },
        }
    )


def test_detector_hostility_ignores_negative_standing_when_rule_disabled(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_standing_threshold=None),
            cooldown_seconds=0,
        ),
    )

    assert not store._active_item_is_hostile(
        {
            "source": "eve-sentry-detector",
            "target_type": "character",
            "system_name": "HB-FSO",
            "name": "Pilot",
            "character_id": 123,
            "metadata": {
                "identity_status": "resolved",
                "character_profiles": [
                    {
                        "character_id": 123,
                        "contact_standing": -5.0,
                    }
                ],
            },
        }
    )


def test_detector_hostility_uses_configured_negative_threshold(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_standing_threshold=-10.0),
            cooldown_seconds=0,
        ),
    )
    base = {
        "source": "eve-sentry-detector",
        "target_type": "character",
        "system_name": "HB-FSO",
        "name": "Pilot",
        "character_id": 123,
        "metadata": {"identity_status": "resolved"},
    }

    below_threshold = {
        **base,
        "metadata": {
            **base["metadata"],
            "character_profiles": [
                {"character_id": 123, "contact_standing": -5.0}
            ],
        },
    }
    at_threshold = {
        **base,
        "metadata": {
            **base["metadata"],
            "character_profiles": [
                {"character_id": 123, "contact_standing": -10.0}
            ],
        },
    }

    assert not store._active_item_is_hostile(below_threshold)
    assert store._active_item_is_hostile(at_threshold)


def test_active_character_summary_keeps_generated_zkill_link(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    summary = store._active_character_profile_summary(
        {
            "character_id": 123,
            "name": "Jone Rayl",
            "zkill_url": "https://zkillboard.com/character/123/",
        }
    )

    assert summary["zkill_url"] == "https://zkillboard.com/character/123/"


def test_record_ocr_snapshot_red_icon_is_persisted_as_direct_alert_evidence(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
    )

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
            "hostile_icon_count": 1,
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")[0]
    observation = store.list_observations(include_suppressed=True)[0]
    alerts = store.list_alerts()

    assert result["created"] == 1
    assert active["metadata"]["hostile_icon_detected"] is True
    assert active["metadata"]["hostile_icon_count"] == 1
    assert observation["metadata"]["hostile_icon_count"] == 1
    assert alerts[0]["score"] == 100
    assert [item["type"] for item in alerts[0]["evidence"]] == [
        "local_ocr_seen",
        "hostile_icon",
    ]


def test_hostile_presence_creates_system_state_without_report_or_alert(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
    )

    result = store.record_hostile_presence(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "system_id": 30004759,
            "hostile_icon_count": 2,
            "seen_at": "2026-08-07T10:00:00+00:00",
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")
    assert result["accepted"] is True
    assert result["created"] == 1
    assert len(active) == 1
    assert active[0]["target_type"] == "system"
    assert active[0]["name"] == ""
    assert active[0]["metadata"]["presence_only"] is True
    assert active[0]["metadata"]["hostile_icon_count"] == 2
    assert store.list_reports() == []
    assert store.list_alerts() == []


def test_hostile_presence_rejects_stale_updates_and_zero_clears_client_state(
    tmp_path,
):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    base = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Pilot",
        "system_name": "S-KSWL",
        "system_id": 30004759,
    }

    store.record_hostile_presence(
        {
            **base,
            "hostile_icon_count": 1,
            "seen_at": "2026-08-07T10:00:00+00:00",
        }
    )
    updated = store.record_hostile_presence(
        {
            **base,
            "hostile_icon_count": 3,
            "seen_at": "2026-08-07T10:00:10+00:00",
        }
    )
    stale = store.record_hostile_presence(
        {
            **base,
            "hostile_icon_count": 0,
            "seen_at": "2026-08-07T10:00:05+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            **base,
            "names": ["Alice"],
            "hostile_icon_count": 3,
            "seen_at": "2026-08-07T10:00:11+00:00",
        }
    )
    cleared = store.record_hostile_presence(
        {
            **base,
            "hostile_icon_count": 0,
            "seen_at": "2026-08-07T10:00:20+00:00",
        }
    )

    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert updated["refreshed"] == 1
    assert stale["accepted"] is False
    assert cleared["accepted"] is True
    assert cleared["expired"] == 2
    assert store.list_active_intel(source="eve-sentry-detector") == []
    assert {item["target_type"] for item in inactive} == {"system", "character"}
    assert all(item["left_at"] == "2026-08-07T10:00:20+00:00" for item in inactive)


def test_ocr_missing_confirmations_do_not_clear_presence_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    base = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Pilot",
        "system_name": "S-KSWL",
    }
    store.record_hostile_presence(
        {
            **base,
            "hostile_icon_count": 2,
            "seen_at": "2026-08-07T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            **base,
            "names": ["Alice"],
            "seen_at": "2026-08-07T10:00:01+00:00",
        }
    )
    for seconds in (9, 17, 25):
        store.record_ocr_snapshot(
            {
                **base,
                "names": [],
                "seen_at": f"2026-08-07T10:00:{seconds:02d}+00:00",
            }
        )

    active = store.list_active_intel(source="eve-sentry-detector")
    assert len(active) == 1
    assert active[0]["target_type"] == "system"
    assert active[0]["metadata"]["hostile_icon_count"] == 2


def test_map_does_not_double_count_presence_and_ocr_for_same_client(tmp_path):
    class HostileResolver:
        def enrich_observation(self, observation):
            observation.character_ids = [101]
            observation.metadata["esi_resolution"] = {
                "attempted": True,
                "resolved_character_names": list(observation.names),
            }
            return observation

        def character_profile(self, character_id):
            return {
                "character_id": int(character_id),
                "name": "Alice",
                "contact_standing": -5.0,
            }

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=HostileResolver(),
    )
    base = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Pilot",
        "system_name": "S-KSWL",
    }
    store.record_hostile_presence(
        {
            **base,
            "hostile_icon_count": 3,
            "seen_at": "2026-08-07T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            **base,
            "names": ["Alice"],
            "hostile_icon_count": 3,
            "seen_at": "2026-08-07T10:00:01+00:00",
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    system = next(
        item for item in store.snapshot()["systems"] if item["name"] == "S-KSWL"
    )
    assert system["hostile_count"] == 3
    assert system["hostiles"] == ["Alice"]


def test_later_red_icon_promotes_an_existing_ocr_sighting_to_alert(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    base_payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot(
        {
            **base_payload,
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    assert store.list_alerts() == []

    store.record_ocr_snapshot(
        {
            **base_payload,
            "seen_at": "2026-07-03T10:00:02+00:00",
            "hostile_icon_count": 1,
        }
    )

    assert len(store.list_alerts()) == 1
    assert store.list_alerts()[0]["score"] == 100


def test_record_ocr_snapshot_stores_esi_identity_metadata(tmp_path):
    class IdentityResolver:
        def enrich_observation(self, observation):
            observation.character_ids = [123]
            observation.metadata = {
                **observation.metadata,
                "esi_resolution": {
                    "attempted": True,
                    "resolved_character_names": ["Alice"],
                    "resolved_character_count": 1,
                },
            }
            return observation

        def character_profile(self, character_id):
            assert character_id == 123
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_id": 42,
                "corporation_name": "Alice Corp",
                "alliance_id": 77,
                "alliance_name": "Alice Alliance",
                "contact_standing": 0.0,
                "standing_source": "character",
            }

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=IdentityResolver(),
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
            "hostile_icon_count": 1,
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    active = store.list_active_intel(source="eve-sentry-detector")[0]
    metadata = active["metadata"]

    assert active["character_id"] == 123
    assert metadata["client_id"] == "detector-client:test"
    assert metadata["character_id"] == 123
    assert metadata["corporation_id"] == 42
    assert metadata["corporation_name"] == "Alice Corp"
    assert metadata["alliance_id"] == 77
    assert metadata["alliance_name"] == "Alice Alliance"
    assert metadata["contact_standing"] == 0.0
    assert metadata["standing_source"] == "character"
    assert metadata["hostile_icon_detected"] is True
    assert metadata["hostile_icon_count"] == 1
    assert metadata["esi_resolution"]["resolved_character_names"] == ["Alice"]
    assert metadata["character_profiles"][0]["name"] == "Alice"


def test_alerts_expose_only_esi_verified_characters(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.add_observation(
        {
            "source": "ocr",
            "system_name": "S-KSWL",
            "names": ["Alice", "Rifter"],
            "character_ids": [123],
            "metadata": {
                "esi_resolution": {
                    "attempted": True,
                    "resolved_character_names": ["Alice"],
                    "unresolved_character_names": ["Rifter"],
                }
            },
        }
    )
    store.add_observation(
        {
            "source": "ocr",
            "system_name": "S-KSWL",
            "names": ["OCR noise"],
            "character_ids": [456],
            "metadata": {
                "esi_resolution": {
                    "attempted": True,
                    "unresolved_character_names": ["OCR noise"],
                }
            },
        }
    )

    alerts = {item["names"][0]: item for item in store.list_alerts()}

    assert alerts["Alice"]["verified_characters"] == [
        {"character_id": 123, "name": "Alice"}
    ]
    assert alerts["OCR noise"]["verified_characters"] == []


def test_alerts_expose_persisted_zkill_stats_for_verified_characters(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.add_observation(
        {
            "source": "ocr",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "character_ids": [123],
            "metadata": {
                "esi_resolution": {
                    "attempted": True,
                    "resolved_character_names": ["Alice"],
                },
                "character_profiles": [
                    {
                        "character_id": 123,
                        "name": "Alice",
                        "zkill": {
                            "source": "zkillboard",
                            "danger_ratio": 84,
                            "ships_destroyed": 320,
                            "fetched_at": "2026-08-03T00:00:00Z",
                        },
                    }
                ],
            },
        }
    )

    alert = store.list_alerts()[0]

    assert alert["verified_characters"] == [
        {
            "character_id": 123,
            "name": "Alice",
            "zkill": {
                "source": "zkillboard",
                "danger_ratio": 84,
                "ships_destroyed": 320,
                "fetched_at": "2026-08-03T00:00:00Z",
            },
        }
    ]


def test_record_ocr_snapshot_does_not_wait_for_esi_resolution(tmp_path):
    class BlockingResolver:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def resolve_names(self, names):
            self.started.set()
            self.release.wait(timeout=2)
            return []

        def enrich_observation(self, observation):
            return observation

    resolver = BlockingResolver()
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=resolver,
    )

    started_at = time.perf_counter()
    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "system_name": "S-KSWL",
            "names": ["Alice"],
        }
    )
    elapsed = time.perf_counter() - started_at

    try:
        assert elapsed < 0.5
        assert result["created"] == 1
        assert resolver.started.wait(timeout=1)
        active = store.list_active_intel(source="eve-sentry-detector")[0]
        assert active["metadata"]["identity_status"] == "pending"
    finally:
        resolver.release.set()

    assert store.wait_for_esi_idle(timeout=2)
    active = store.list_active_intel(source="eve-sentry-detector")[0]
    assert active["metadata"]["identity_status"] == "unresolved"


def test_record_ocr_snapshot_uses_fresh_cached_identity_without_queueing(tmp_path):
    class NetworkMustNotRun:
        def resolve_ids(self, names):
            raise AssertionError(names)

        def get_character(self, character_id):
            raise AssertionError(character_id)

    class EnricherMustNotRun:
        esi_session = None

        def enrich(self, observation):
            raise AssertionError(observation)

    cache = EsiCache(tmp_path / "esi.json")
    cache.set(
        "name:alice",
        {"name": "Alice", "category": "character", "id": 123},
    )
    cache.set(
        "character:123",
        {
            "character_id": 123,
            "name": "Alice",
            "corporation_id": 456,
            "corporation_name": "Some Corp",
            "contact_standing": -10.0,
        },
    )
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=EsiResolver(client=NetworkMustNotRun(), cache=cache),
        enricher=EnricherMustNotRun(),
    )
    submitted = []
    store._esi_worker.submit = (
        lambda key, task: submitted.append((key, task)) or True
    )

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "system_name": "S-KSWL",
            "names": ["Alice"],
        }
    )
    active = store.list_active_intel(source="eve-sentry-detector")[0]

    assert result["created"] == 1
    assert submitted == []
    assert active["character_id"] == 123
    assert active["metadata"]["identity_status"] == "resolved"
    assert active["metadata"]["corporation_name"] == "Some Corp"
    assert active["metadata"]["contact_standing"] == -10.0


def test_record_ocr_snapshot_uses_stale_identity_then_queues_refresh(tmp_path):
    cache = EsiCache(tmp_path / "esi.json")
    cache._items = {
        "name:alice": {
            "value": {"name": "Alice", "category": "character", "id": 123},
            "expires_at": 0,
        },
        "character:123": {
            "value": {"character_id": 123, "name": "Alice"},
            "expires_at": 0,
        },
    }
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=EsiResolver(client=object(), cache=cache),
    )
    submitted = []
    store._esi_worker.submit = (
        lambda key, task: submitted.append((key, task)) or True
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "system_name": "S-KSWL",
            "names": ["Alice"],
        }
    )
    active = store.list_active_intel(source="eve-sentry-detector")[0]

    assert active["character_id"] == 123
    assert active["metadata"]["identity_status"] == "resolved"
    assert active["metadata"]["esi_resolution"]["cache_status"] == "stale"
    assert len(submitted) == 1
    assert submitted[0][1].force_profile_refresh is True


def test_record_ocr_snapshot_uses_fresh_negative_cache_without_queueing(tmp_path):
    cache = EsiCache(tmp_path / "esi.json")
    cache.set("name:ocr noise", {"name": "OCR Noise", "status": "not_found"})
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=EsiResolver(client=object(), cache=cache),
    )
    submitted = []
    store._esi_worker.submit = (
        lambda key, task: submitted.append((key, task)) or True
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "system_name": "S-KSWL",
            "names": ["OCR Noise"],
        }
    )
    active = store.list_active_intel(source="eve-sentry-detector")[0]

    assert submitted == []
    assert active["character_id"] is None
    assert active["metadata"]["identity_status"] == "unresolved"


def test_delayed_esi_result_does_not_restore_stale_hostile_count(tmp_path):
    class BlockingResolver:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def resolve_names(self, names):
            self.started.set()
            self.release.wait(timeout=2)
            return []

        def enrich_observation(self, observation):
            return observation

    resolver = BlockingResolver()
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=resolver,
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-24T09:09:16+00:00",
            "names": ["Shisen Hanomaa"],
            "hostile_icon_count": 1,
        }
    )
    assert resolver.started.wait(timeout=1)

    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-24T09:09:18+00:00",
            "names": [],
        }
    )
    resolver.release.set()
    assert store.wait_for_esi_idle(timeout=2)

    active = store.list_active_intel(source="eve-sentry-detector")[0]
    assert active["metadata"]["hostile_icon_detected"] is False
    assert active["metadata"]["hostile_icon_count"] == 0
    assert active["metadata"]["hostile_icon_seen_at"] == (
        "2026-07-24T09:09:18+00:00"
    )
    system = next(
        item for item in store.snapshot()["systems"]
        if item["name"] == "S-KSWL"
    )
    assert system["hostile_count"] == 0


def test_record_ocr_snapshot_skips_identity_refresh_for_active_duplicates(tmp_path):
    class IdentityResolver:
        def __init__(self):
            self.enrich_calls = 0

        def enrich_observation(self, observation):
            self.enrich_calls += 1
            observation.character_ids = [123]
            return observation

        def character_profile(self, character_id):
            return {"character_id": int(character_id), "name": "Alice"}

    class StandingEnricher:
        def __init__(self):
            self.calls = 0

        def enrich(self, observation):
            self.calls += 1
            profile = {
                "character_id": observation.character_ids[0],
                "name": "Alice",
                "corporation_id": 42,
                "corporation_name": "Alice Corp",
            }
            if self.calls >= 2:
                profile["contact_standing"] = 0.0
                profile["standing_source"] = "character"
            return SimpleNamespace(character_profiles=[profile])

    resolver = IdentityResolver()
    enricher = StandingEnricher()
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=resolver,
        enricher=enricher,
    )
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:00+00:00"}
    )
    assert store.wait_for_esi_idle(timeout=1)
    first = store.list_active_intel(source="eve-sentry-detector")[0]
    assert "contact_standing" not in first["metadata"]
    resolver_calls_after_create = resolver.enrich_calls
    enricher_calls_after_create = enricher.calls

    second = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:01:01+00:00"}
    )
    refreshed = store.list_active_intel(source="eve-sentry-detector")[0]

    assert second["refreshed"] == 1
    assert resolver.enrich_calls == resolver_calls_after_create
    assert enricher.calls == enricher_calls_after_create
    assert "contact_standing" not in refreshed["metadata"]
    assert refreshed["metadata"]["corporation_name"] == "Alice Corp"


def test_record_ocr_snapshot_filters_friendly_corporation_from_active_intel(tmp_path):
    class FriendlyResolver:
        def enrich_observation(self, observation):
            if observation.names == ["Alice"]:
                observation.character_ids = [123]
            return observation

        def character_profile(self, character_id):
            return {
                "character_id": int(character_id),
                "name": "Alice",
                "corporation_id": 42,
            }

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=FriendlyResolver(),
        scorer=ScoringEngine(
            watchlist=Watchlist(friendly_corporation_ids={42}),
            cooldown_seconds=0,
        ),
    )

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    assert result["filtered"] == 0
    assert result["created"] == 1
    assert store.wait_for_esi_idle(timeout=1)

    assert store.list_active_intel(source="eve-sentry-detector") == []
    assert store.list_alerts() == []
    assert store.list_observations()[0]["character_ids"] == [123]


def test_record_ocr_snapshot_hides_whitelisted_names_from_default_lists(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(whitelist={"Alice"}),
            cooldown_seconds=0,
        ),
    )

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    snapshot = store.snapshot()

    assert result["filtered"] == 1
    assert store.list_active_intel(source="eve-sentry-detector") == []
    assert store.list_alerts() == []
    assert store.list_reports() == []
    assert store.list_observations() == []
    assert store.list_observations(include_suppressed=True)[0]["names"] == ["Alice"]
    assert snapshot["reports"] == []
    assert snapshot["observations"] == []


def test_record_ocr_snapshot_canonicalizes_leading_i_l_ocr_name(tmp_path):
    class FakeResolver:
        def __init__(self):
            self.resolve_calls = []
            self.profile_calls = []

        def resolve_names(self, names):
            self.resolve_calls.append(list(names))
            if names == ["lona Gonemion", "Iona Gonemion"]:
                return [
                    SimpleNamespace(
                        name="Iona Gonemion",
                        category="character",
                        entity_id=90621602,
                    )
                ]
            return []

        def character_profile(self, character_id):
            self.profile_calls.append(int(character_id))
            return {
                "character_id": int(character_id),
                "name": "Iona Gonemion",
                "corporation_id": 98530802,
                "alliance_id": 99003581,
            }

        def enrich_observation(self, observation):
            if observation.names == ["Iona Gonemion"]:
                observation.character_ids = [90621602]
            return observation

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=resolver,
    )
    payload = {
        "client_id": "detector:test",
        "system_name": "S-KSWL",
        "names": ["lona Gonemion"],
        "seen_at": "2026-07-03T10:00:00+00:00",
    }

    store.record_ocr_snapshot(payload)
    store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:02+00:00"}
    )
    assert store.wait_for_esi_idle(timeout=1)

    active = store.list_active_intel()
    assert active[0]["name"] == "Iona Gonemion"
    assert active[0]["character_id"] == 90621602
    assert active[0]["seen_count"] == 1
    assert store.list_observations(include_suppressed=True)[0]["names"] == [
        "Iona Gonemion"
    ]
    assert resolver.resolve_calls == [["lona Gonemion", "Iona Gonemion"]]
    assert resolver.profile_calls == [90621602]


def test_record_ocr_snapshot_keeps_exact_i_l_name_when_esi_resolves_it(tmp_path):
    class FakeResolver:
        def __init__(self):
            self.resolve_calls = []

        def resolve_names(self, names):
            self.resolve_calls.append(list(names))
            return [
                SimpleNamespace(
                    name="lona Gonemion",
                    category="character",
                    entity_id=90000001,
                ),
                SimpleNamespace(
                    name="Iona Gonemion",
                    category="character",
                    entity_id=90621602,
                ),
            ]

        def character_profile(self, character_id):
            return {"character_id": int(character_id), "name": "lona Gonemion"}

        def enrich_observation(self, observation):
            if observation.names == ["lona Gonemion"]:
                observation.character_ids = [90000001]
            return observation

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=FakeResolver(),
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector:test",
            "system_name": "S-KSWL",
            "names": ["lona Gonemion"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    active = store.list_active_intel()
    assert active[0]["name"] == "lona Gonemion"
    assert active[0]["character_id"] == 90000001


def test_record_ocr_snapshot_canonicalizes_zero_letter_ocr_name(tmp_path):
    class FakeResolver:
        def __init__(self):
            self.resolve_calls = []

        def resolve_names(self, names):
            self.resolve_calls.append(list(names))
            if names == ["LOSER18q", "L0SER18q"]:
                return [
                    SimpleNamespace(
                        name="L0SER18q",
                        category="character",
                        entity_id=331832684,
                    )
                ]
            return []

        def character_profile(self, character_id):
            return {
                "character_id": int(character_id),
                "name": "L0SER18q",
                "corporation_id": 456,
                "alliance_id": 789,
            }

        def enrich_observation(self, observation):
            if observation.names == ["L0SER18q"]:
                observation.character_ids = [331832684]
            return observation

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=resolver,
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector:test",
            "system_name": "S-KSWL",
            "names": ["LOSER18q"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    active = store.list_active_intel()
    assert active[0]["name"] == "L0SER18q"
    assert active[0]["character_id"] == 331832684
    assert store.list_observations(include_suppressed=True)[0]["names"] == [
        "L0SER18q"
    ]
    assert resolver.resolve_calls == [["LOSER18q"], ["LOSER18q", "L0SER18q"]]


def test_record_ocr_snapshot_resolves_new_names_without_i_l_once(tmp_path):
    class FakeResolver:
        def __init__(self):
            self.resolve_calls = []
            self.profile_calls = []

        def resolve_names(self, names):
            self.resolve_calls.append(list(names))
            if names == ["Bob"]:
                return [
                    SimpleNamespace(
                        name="Bob",
                        category="character",
                        entity_id=123,
                    )
                ]
            return []

        def character_profile(self, character_id):
            self.profile_calls.append(int(character_id))
            return {
                "character_id": int(character_id),
                "name": "Bob",
                "corporation_id": 456,
                "alliance_id": 789,
            }

        def enrich_observation(self, observation):
            if observation.names == ["Bob"]:
                observation.character_ids = [123]
            return observation

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=resolver,
    )
    payload = {
        "client_id": "detector:test",
        "system_name": "S-KSWL",
        "names": ["Bob"],
        "seen_at": "2026-07-03T10:00:00+00:00",
    }

    store.record_ocr_snapshot(payload)
    store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:02+00:00"}
    )
    assert store.wait_for_esi_idle(timeout=1)

    active = store.list_active_intel()
    assert active[0]["name"] == "Bob"
    assert active[0]["character_id"] == 123
    assert active[0]["seen_count"] == 1
    assert resolver.resolve_calls == [["Bob"]]
    assert resolver.profile_calls == [123]


def test_record_ocr_snapshot_only_confuses_upper_i_with_lower_l(tmp_path):
    class FakeResolver:
        def __init__(self):
            self.resolve_calls = []

        def resolve_names(self, names):
            self.resolve_calls.append(list(names))
            return []

        def enrich_observation(self, observation):
            return observation

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=resolver,
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector:test",
            "system_name": "S-KSWL",
            "names": ["Mira LName"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    assert resolver.resolve_calls == [["Mira LName"]]


def test_record_ocr_snapshot_uses_esi_completion_after_exact_lookup_misses(tmp_path):
    events = []
    full_name = "Kamamdzhava Tekerav Longname"

    class EmptyResolver:
        def resolve_names(self, names):
            events.append(("exact", list(names)))
            return []

        def enrich_observation(self, observation):
            return observation

    class CompletingEnricher:
        def complete_character_name(self, prefix):
            events.append(("complete", prefix))
            return full_name

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=EmptyResolver(),
        enricher=CompletingEnricher(),
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector:test",
            "system_name": "S-KSWL",
            "names": ["Kamamdzhava Teker"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    assert store.list_observations()[0]["names"] == [full_name]
    assert events[:2] == [
        ("exact", ["Kamamdzhava Teker"]),
        ("complete", "Kamamdzhava Teker"),
    ]


def test_record_ocr_snapshot_skips_completion_after_exact_esi_match(tmp_path):
    class ExactResolver:
        def resolve_names(self, names):
            return [
                SimpleNamespace(
                    name="Kamamdzhava Teker",
                    category="character",
                    entity_id=456,
                )
            ]

        def character_profile(self, character_id):
            return {"character_id": int(character_id), "name": "Kamamdzhava Teker"}

        def enrich_observation(self, observation):
            observation.character_ids = [456]
            return observation

    class FailingEnricher:
        def complete_character_name(self, prefix):
            raise AssertionError(f"completion must not run for exact match: {prefix}")

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=ExactResolver(),
        enricher=FailingEnricher(),
    )

    store.record_ocr_snapshot(
        {
            "client_id": "detector:test",
            "system_name": "S-KSWL",
            "names": ["Kamamdzhava Teker"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    assert store.wait_for_esi_idle(timeout=1)

    assert store.list_observations()[0]["names"] == ["Kamamdzhava Teker"]
    assert store.list_observations()[0]["character_ids"] == [456]


def test_record_ocr_snapshot_filters_positive_esi_corporation_standing(tmp_path):
    class FriendlyResolver:
        def enrich_observation(self, observation):
            if observation.names == ["Alice"]:
                observation.character_ids = [123]
            return observation

        def character_profile(self, character_id):
            return {
                "character_id": int(character_id),
                "name": "Alice",
                "corporation_id": 42,
            }

    class FriendlySession:
        def snapshot(self, include_location=True, include_contacts=True):
            return SimpleNamespace(
                contacts=[
                    {
                        "contact_id": 42,
                        "contact_type": "corporation",
                        "standing": 10.0,
                    }
                ]
            )

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=FriendlyResolver(),
        scorer=ScoringEngine(cooldown_seconds=0),
        enricher=ThreatEnricher(
            resolver=FriendlyResolver(),
            esi_session=FriendlySession(),
        ),
    )

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    assert result["filtered"] == 0
    assert result["created"] == 1
    assert store.wait_for_esi_idle(timeout=1)

    assert store.list_active_intel(source="eve-sentry-detector") == []
    assert store.list_alerts() == []
    assert store.list_observations()[0]["character_ids"] == [123]


def test_channel_observation_creates_ttl_active_intel(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout"},
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )

    active = store.list_active_intel(source="intel_channel")

    assert len(active) == 1
    assert active[0]["system_name"] == "S-KSWL"
    assert active[0]["expires_at"] == "2099-07-03T10:03:00+00:00"
    assert active[0]["source_observation_ids"] == [observation.observation_id]


def test_channel_observation_filters_friendly_alliance_from_active_intel(tmp_path):
    class FriendlyResolver:
        def enrich_observation(self, observation):
            if observation.names == ["Alice"]:
                observation.character_ids = [123]
            return observation

        def character_profile(self, character_id):
            return {
                "character_id": int(character_id),
                "name": "Alice",
                "alliance_id": 77,
            }

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=FriendlyResolver(),
        scorer=ScoringEngine(
            watchlist=Watchlist(friendly_alliance_ids={77}),
            cooldown_seconds=0,
        ),
    )

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "raw_text": "Scout: S-KSWL Alice",
            "metadata": {"hostile_count": 1, "sender": "Scout"},
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )

    assert observation.character_ids == [123]
    assert store.list_active_intel(source="intel_channel") == []
    assert store.list_alerts() == []
    assert len(store.list_observations(source="intel_channel")) == 1


def test_snapshot_alerts_include_only_active_intel_sources(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    active_observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "raw_text": "Scout: S-KSWL Alice",
            "metadata": {"hostile_count": 1, "sender": "Scout"},
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    expired_observation = store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "N5Y-4N",
            "names": ["Bob"],
            "raw_text": "Scout: N5Y-4N Bob",
            "metadata": {"hostile_count": 1, "sender": "Scout"},
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )

    snapshot = store.snapshot()
    historical_alert_ids = {
        item["source_observation_id"] for item in store.list_alerts()
    }

    assert [item["source_observation_id"] for item in snapshot["alerts"]] == [
        active_observation.observation_id
    ]
    assert snapshot["summary"]["alert_count"] == 1
    assert active_observation.observation_id in historical_alert_ids
    assert expired_observation.observation_id in historical_alert_ids


def test_channel_clear_deactivates_matching_system_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout"},
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL clr",
            "seen_at": "2026-07-03T10:01:00+00:00",
        }
    )

    assert store.list_active_intel(source="intel_channel") == []
    inactive = store.list_active_intel(source="intel_channel", active=False)
    assert inactive[0]["cleared_at"] == "2026-07-03T10:01:00+00:00"


def test_channel_clear_does_not_deactivate_unrelated_active_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout"},
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Branch",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL clr",
            "seen_at": "2099-07-03T10:01:00+00:00",
        }
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "N5Y-4N",
            "raw_text": "Scout: N5Y-4N clr",
            "seen_at": "2099-07-03T10:02:00+00:00",
        }
    )

    active = store.list_active_intel(source="intel_channel")

    assert len(active) == 1
    assert active[0]["source_instance"] == "wc.Venal"
    assert active[0]["system_name"] == "S-KSWL"


def test_channel_active_intel_expires_without_deleting_observation(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout"},
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )

    active = store.list_active_intel(source="intel_channel")
    assert len(active) == 1
    assert active[0]["expires_at"] == "2099-07-03T10:03:00+00:00"

    store.expire_active_intel("2099-07-03T10:03:01+00:00")

    assert store.list_active_intel(source="intel_channel") == []
    inactive = store.list_active_intel(source="intel_channel", active=False)
    assert inactive[0]["left_at"] == "2099-07-03T10:03:01+00:00"
    assert len(store.list_observations(source="intel_channel")) == 1


def test_stale_duplicate_channel_threat_after_clear_does_not_reactivate(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    threat = {
        "source": "intel_channel",
        "source_instance": "wc.Venal",
        "system_name": "S-KSWL",
        "raw_text": "Scout: S-KSWL +3 reds",
        "metadata": {"hostile_count": 3, "sender": "Scout"},
        "seen_at": "2026-07-03T10:00:00+00:00",
    }
    store.add_observation(threat)
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "wc.Venal",
            "system_name": "S-KSWL",
            "raw_text": "Scout: S-KSWL clr",
            "seen_at": "2026-07-03T10:01:00+00:00",
        }
    )

    store.add_observation({**threat, "id": "replayed-old-threat"})

    assert store.list_active_intel(source="intel_channel") == []
    assert len(store.list_observations(source="intel_channel")) == 2


def test_stale_duplicate_channel_clear_does_not_clear_newer_threat(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    base_threat = {
        "source": "intel_channel",
        "source_instance": "wc.Venal",
        "system_name": "S-KSWL",
        "raw_text": "Scout: S-KSWL +3 reds",
        "metadata": {"hostile_count": 3, "sender": "Scout"},
    }
    old_clear = {
        "source": "intel_channel",
        "source_instance": "wc.Venal",
        "system_name": "S-KSWL",
        "raw_text": "Scout: S-KSWL clr",
        "seen_at": "2099-07-03T10:01:00+00:00",
    }

    store.add_observation({**base_threat, "seen_at": "2099-07-03T10:00:00+00:00"})
    store.add_observation(old_clear)
    store.add_observation({**base_threat, "seen_at": "2099-07-03T10:02:00+00:00"})

    store.add_observation({**old_clear, "id": "replayed-old-clear"})

    active = store.list_active_intel(source="intel_channel")
    assert len(active) == 1
    assert active[0]["last_seen_at"] == "2099-07-03T10:02:00+00:00"
    assert active[0]["system_name"] == "S-KSWL"


def test_record_ocr_snapshot_refreshes_when_source_instance_changes(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot(
        {
            **payload,
            "source_instance": "EVE - Old",
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    second = store.record_ocr_snapshot(
        {
            **payload,
            "source_instance": "EVE - New",
            "seen_at": "2026-07-03T10:00:02+00:00",
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")

    assert second["refreshed"] == 1
    assert len(active) == 1
    assert len(store.list_observations()) == 1
    assert active[0]["source_instance"] == "EVE - New"


def test_record_ocr_snapshot_does_not_rewind_last_seen_at(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot({**payload, "seen_at": "2026-07-03T10:00:10+00:00"})
    second = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:02+00:00"}
    )
    still_active = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:14+00:00", "names": []}
    )

    active = store.list_active_intel(source="eve-sentry-detector")

    assert second["refreshed"] == 1
    assert active[0]["last_seen_at"] == "2026-07-03T10:00:10+00:00"
    assert still_active["expired"] == 0
    assert still_active["missing"] == 1


def test_record_ocr_snapshot_rejects_invalid_seen_at(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    with pytest.raises(ValueError):
        store.record_ocr_snapshot(
            {
                "client_id": "detector-client:test",
                "system_name": "S-KSWL",
                "seen_at": "not-a-timestamp",
                "names": ["Alice"],
            }
        )


def test_record_ocr_snapshot_deduplicates_names_case_insensitively(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice", "alice"],
        }
    )
    active = store.list_active_intel(source="eve-sentry-detector")

    assert result["created"] == 1
    assert len(active) == 1
    assert len(store.list_observations()) == 1
    assert active[0]["name"] == "Alice"


def test_record_ocr_snapshot_filters_numeric_member_count_noise(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["二8", "二 6", "Alice"],
        }
    )

    assert result["created"] == 1
    assert [item["name"] for item in store.list_active_intel()] == ["Alice"]


def test_record_ocr_snapshot_filters_distance_and_location_marker_noise(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["527 m", "95 km", "0.8 AU", "R-YWID*", "Alice"],
        }
    )

    assert result["created"] == 1
    assert [item["name"] for item in store.list_active_intel()] == ["Alice"]


def test_record_ocr_snapshot_case_change_does_not_mark_name_missing(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
    }

    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    second = store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:02+00:00",
            "names": ["alice"],
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")

    assert second["refreshed"] == 1
    assert second["missing"] == 0
    assert len(active) == 1


def test_record_ocr_snapshot_expires_missing_names_after_grace_period(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot({**payload, "seen_at": "2026-07-03T10:00:00+00:00"})
    still_active = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:04+00:00", "names": []}
    )
    still_confirming = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:08+00:00", "names": []}
    )
    expired = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:12+00:00", "names": []}
    )

    assert still_active["missing"] == 1
    assert still_active["expired"] == 0
    assert still_confirming["missing"] == 1
    assert still_confirming["expired"] == 0
    assert expired["expired"] == 1
    assert store.list_active_intel() == []
    assert store.list_active_intel(active=False)[0]["left_at"] == (
        "2026-07-03T10:00:12+00:00"
    )


def test_record_ocr_snapshot_resets_missing_confirmation_when_name_returns(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
    }
    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice", "Bob"],
        }
    )
    store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:08+00:00", "names": ["Alice"]}
    )
    store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:16+00:00", "names": ["Alice", "Bob"]}
    )
    store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:24+00:00", "names": ["Alice"]}
    )
    second_missing = store.record_ocr_snapshot(
        {**payload, "seen_at": "2026-07-03T10:00:32+00:00", "names": ["Alice"]}
    )

    assert second_missing["missing"] == 1
    assert second_missing["expired"] == 0
    assert {item["name"] for item in store.list_active_intel()} == {"Alice", "Bob"}


def test_confirmed_ocr_departure_resets_alert_cooldown_for_reentry(tmp_path):
    clock = [1000.0]
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(blacklist={"Alice"}),
            cooldown_seconds=60,
            now=lambda: clock[0],
        ),
    )
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
    }

    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    first_alerts = store.list_alerts()
    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:08+00:00",
            "names": [],
        }
    )
    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:16+00:00",
            "names": [],
        }
    )
    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:24+00:00",
            "names": [],
        }
    )
    store.record_ocr_snapshot(
        {
            **payload,
            "seen_at": "2026-07-03T10:00:25+00:00",
            "names": ["Alice"],
        }
    )
    reentry_alerts = store.list_alerts()

    assert len(first_alerts) == 1
    assert len(reentry_alerts) == 2
    assert {
        alert["source_observation_id"] for alert in reentry_alerts
    } != {first_alerts[0]["source_observation_id"]}


def test_record_ocr_snapshot_isolates_missing_names_by_client_id(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    base = {
        "system_name": "S-KSWL",
        "names": ["Alice"],
    }

    store.record_ocr_snapshot(
        {
            **base,
            "client_id": "detector-client:test:eve-pilot-a",
            "source_instance": "EVE - Pilot A",
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            **base,
            "client_id": "detector-client:test:eve-pilot-b",
            "source_instance": "EVE - Pilot B",
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:eve-pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:08+00:00",
            "names": [],
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:eve-pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:16+00:00",
            "names": [],
        }
    )
    expired = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:eve-pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:24+00:00",
            "names": [],
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")
    inactive = store.list_active_intel(source="eve-sentry-detector", active=False)

    assert expired["expired"] == 1
    assert len(active) == 1
    assert active[0]["source_instance"] == "EVE - Pilot B"
    assert active[0]["metadata"] == {
        "client_id": "detector-client:test:eve-pilot-b"
    }
    assert len(inactive) == 1
    assert inactive[0]["source_instance"] == "EVE - Pilot A"


def test_detector_stopped_heartbeat_expires_ocr_active_intel_after_grace(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )

    heartbeat = store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "idle",
            "seen_at": "2099-07-03T10:00:05+00:00",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )

    assert heartbeat["status"] == "idle"
    store.expire_active_intel("2099-07-03T10:00:08+00:00")
    active = store.list_active_intel(source="eve-sentry-detector")
    assert [item["name"] for item in active] == ["Alice"]
    assert store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    ) == []

    assert store.expire_active_intel("2099-07-03T10:00:12+00:00") == 1
    assert store.list_active_intel(source="eve-sentry-detector") == []
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert [item["name"] for item in inactive] == ["Alice"]
    assert inactive[0]["metadata"]["left_reason"] == "monitor_stopped"


def test_detector_stopped_heartbeat_expires_window_scoped_presence(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_hostile_presence(
        {
            "client_id": "detector-client:test:hwnd-123-eve-pilot",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )

    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "idle",
            "seen_at": "2099-07-03T10:00:05+00:00",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )

    assert store.expire_active_intel("2099-07-03T10:00:40+00:00") == 1
    assert store.list_active_intel(source="eve-sentry-detector") == []
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert inactive[0]["metadata"]["client_id"] == (
        "detector-client:test:hwnd-123-eve-pilot"
    )
    assert inactive[0]["metadata"]["hostile_icon_count"] == 1
    assert inactive[0]["metadata"]["left_reason"] == "monitor_stopped"
    assert inactive[0]["metadata"]["presence_only"] is True


def test_repeated_stopped_heartbeat_does_not_extend_presence_grace(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_hostile_presence(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    heartbeat = {
        "client_id": "detector-client:test",
        "client_type": "detector_client",
        "status": "idle",
        "details": {"monitoring": False, "last_action": "monitor_stopped"},
    }
    store.record_heartbeat(
        heartbeat | {"seen_at": "2099-07-03T10:00:01+00:00"}
    )
    store.record_heartbeat(
        heartbeat | {"seen_at": "2099-07-03T10:00:06+00:00"}
    )

    assert store.expire_active_intel("2099-07-03T10:00:08+00:00") == 1
    assert store.list_active_intel(source="eve-sentry-detector") == []
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert inactive[0]["left_at"] == "2099-07-03T10:00:07+00:00"
    assert inactive[0]["metadata"]["left_reason"] == "monitor_stopped"


def test_stopped_state_survives_later_diagnostic_heartbeat(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_hostile_presence(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    common = {
        "client_id": "detector-client:test",
        "client_type": "detector_client",
        "status": "idle",
    }
    store.record_heartbeat(
        common
        | {
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )
    heartbeat = store.record_heartbeat(
        common
        | {
            "seen_at": "2099-07-03T10:00:06+00:00",
            "details": {"monitoring": False, "last_action": "ocr_snapshot:1"},
        }
    )

    assert heartbeat["details"] == {
        "monitoring": False,
        "last_action": "ocr_snapshot:1",
    }
    assert store.expire_active_intel("2099-07-03T10:00:08+00:00") == 1
    assert store.list_active_intel(source="eve-sentry-detector") == []


def test_idle_without_explicit_stop_does_not_expire_detector_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "names": ["Alice"],
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "idle",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {"monitoring": False, "last_action": "startup_idle"},
        }
    )

    assert store.expire_active_intel("2099-07-03T10:01:00+00:00") == 0
    assert [
        item["name"]
        for item in store.list_active_intel(source="eve-sentry-detector")
    ] == ["Alice"]


def test_monitoring_resume_clears_stable_stopped_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_hostile_presence(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    common = {
        "client_id": "detector-client:test",
        "client_type": "detector_client",
    }
    store.record_heartbeat(
        common
        | {
            "status": "idle",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )
    store.record_heartbeat(
        common
        | {
            "status": "running",
            "seen_at": "2099-07-03T10:00:02+00:00",
            "details": {"monitoring": True, "last_action": "monitor_started"},
        }
    )

    assert store.expire_active_intel("2099-07-03T10:01:00+00:00") == 0
    assert len(store.list_active_intel(source="eve-sentry-detector")) == 1


def test_stopped_parent_expires_ocr_and_presence_without_touching_other_parent(
    tmp_path,
):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_hostile_presence(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "names": ["Good Spirit"],
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_hostile_presence(
        {
            "client_id": "detector-client:other:pilot-b",
            "source_instance": "EVE - Pilot B",
            "system_name": "S-KSWL",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "idle",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )
    store.record_heartbeat(
        {
            "client_id": "detector-client:other",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {"monitoring": True, "last_action": "ocr_snapshot:1"},
        }
    )

    assert store.expire_active_intel("2099-07-03T10:00:08+00:00") == 2
    active = store.list_active_intel(source="eve-sentry-detector")
    assert len(active) == 1
    assert active[0]["metadata"]["client_id"] == "detector-client:other:pilot-b"
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert {item["target_type"] for item in inactive} == {"character", "system"}
    assert {
        item["metadata"]["client_id"]
        for item in inactive
    } == {"detector-client:test:pilot-a"}


def test_detector_heartbeat_target_flags_do_not_change_ocr_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    for client_id, title in [
        ("detector-client:test:pilot-a", "EVE - Pilot A"),
        ("detector-client:test:pilot-b", "EVE - Pilot B"),
    ]:
        store.record_ocr_snapshot(
            {
                "client_id": client_id,
                "source_instance": title,
                "system_name": "S-KSWL",
                "names": ["Alice"],
                "seen_at": "2099-07-03T10:00:00+00:00",
            }
        )

    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-07-03T10:00:05+00:00",
            "details": {
                "monitoring": True,
                "targets": [
                    {
                        "client_id": "detector-client:test:pilot-a",
                        "monitoring": False,
                    },
                    {
                        "client_id": "detector-client:test:pilot-b",
                        "monitoring": True,
                    },
                ],
            },
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")
    inactive = store.list_active_intel(source="eve-sentry-detector", active=False)
    assert [item["source_instance"] for item in active] == [
        "EVE - Pilot A",
        "EVE - Pilot B",
    ]
    assert inactive == []


def test_authoritative_targets_expire_only_removed_child_state(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    old_client_id = "detector-client:test:user-100"
    current_client_id = "detector-client:test:user-200"
    store.record_hostile_presence(
        {
            "client_id": old_client_id,
            "source_instance": "EVE - Old Pilot",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": old_client_id,
            "source_instance": "EVE - Old Pilot",
            "system_name": "HB-FSO",
            "names": ["Good Spirit"],
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_hostile_presence(
        {
            "client_id": current_client_id,
            "source_instance": "EVE - Current Pilot",
            "system_name": "S-KSWL",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )

    heartbeat = store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {
                "monitoring": True,
                "targets": [
                    {
                        "client_id": current_client_id,
                        "system_name": "S-KSWL",
                        "monitoring": True,
                        "capture_online": True,
                    }
                ],
                "_server_monitor_target_ids": [old_client_id],
                "_server_missing_targets": {
                    current_client_id: "2000-01-01T00:00:00+00:00"
                },
            },
        }
    )

    assert not any(key.startswith("_server_") for key in heartbeat["details"])
    assert store.expire_active_intel("2099-07-03T10:00:08+00:00") == 2
    active = store.list_active_intel(source="eve-sentry-detector")
    assert {item["metadata"]["client_id"] for item in active} == {
        current_client_id
    }
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert {item["target_type"] for item in inactive} == {"character", "system"}
    assert {item["metadata"]["client_id"] for item in inactive} == {
        old_client_id
    }
    assert {item["metadata"]["left_reason"] for item in inactive} == {
        "target_removed"
    }


def test_authoritative_targets_keep_all_listed_windows_even_capture_offline(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    client_ids = [
        "detector-client:test:user-100",
        "detector-client:test:user-200",
    ]
    for client_id in client_ids:
        store.record_hostile_presence(
            {
                "client_id": client_id,
                "source_instance": client_id,
                "system_name": "S-KSWL",
                "hostile_icon_count": 1,
                "seen_at": "2099-07-03T10:00:00+00:00",
            }
        )

    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {
                "monitoring": True,
                "targets": [
                    {
                        "client_id": client_ids[0],
                        "monitoring": True,
                        "capture_online": True,
                    },
                    {
                        "client_id": client_ids[1],
                        "monitoring": True,
                        "capture_online": False,
                    },
                ],
            },
        }
    )

    assert store.expire_active_intel("2099-07-03T10:01:00+00:00") == 0
    active = store.list_active_intel(source="eve-sentry-detector")
    assert {item["metadata"]["client_id"] for item in active} == set(client_ids)


@pytest.mark.parametrize(
    "details",
    [
        {"monitoring": True},
        {"monitoring": True, "targets": []},
        {"monitoring": True, "targets": [{"system_name": "S-KSWL"}]},
    ],
)
def test_non_authoritative_targets_do_not_expire_child_state(tmp_path, details):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_hostile_presence(
        {
            "client_id": "detector-client:test:user-100",
            "source_instance": "EVE - Pilot",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": details,
        }
    )

    assert store.expire_active_intel("2099-07-03T10:01:00+00:00") == 0
    assert len(store.list_active_intel(source="eve-sentry-detector")) == 1


def test_removed_target_first_seen_is_stable_across_repeated_heartbeats(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    old_client_id = "detector-client:test:user-100"
    current_client_id = "detector-client:test:user-200"
    store.record_hostile_presence(
        {
            "client_id": old_client_id,
            "source_instance": "EVE - Old Pilot",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    heartbeat = {
        "client_id": "detector-client:test",
        "client_type": "detector_client",
        "status": "running",
        "details": {
            "monitoring": True,
            "targets": [{"client_id": current_client_id}],
        },
    }
    store.record_heartbeat(
        heartbeat | {"seen_at": "2099-07-03T10:00:01+00:00"}
    )
    store.record_heartbeat(
        heartbeat | {"seen_at": "2099-07-03T10:00:06+00:00"}
    )

    assert store.expire_active_intel("2099-07-03T10:00:08+00:00") == 1
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert inactive[0]["left_at"] == "2099-07-03T10:00:07+00:00"
    assert inactive[0]["metadata"]["left_reason"] == "target_removed"


def test_removed_target_late_upload_extends_grace_then_expires(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    old_client_id = "detector-client:test:user-100"
    current_client_id = "detector-client:test:user-200"
    store.record_ocr_snapshot(
        {
            "client_id": old_client_id,
            "source_instance": "EVE - Old Pilot",
            "system_name": "HB-FSO",
            "names": ["Good Spirit"],
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {
                "monitoring": True,
                "targets": [{"client_id": current_client_id}],
            },
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": old_client_id,
            "source_instance": "EVE - Old Pilot",
            "system_name": "HB-FSO",
            "names": ["Good Spirit"],
            "seen_at": "2099-07-03T10:00:08+00:00",
        }
    )

    assert store.expire_active_intel("2099-07-03T10:00:12+00:00") == 0
    assert store.expire_active_intel("2099-07-03T10:00:15+00:00") == 1
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert inactive[0]["left_at"] == "2099-07-03T10:00:14+00:00"


def test_removed_target_reappearing_cancels_pending_cleanup(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    old_client_id = "detector-client:test:user-100"
    current_client_id = "detector-client:test:user-200"
    store.record_hostile_presence(
        {
            "client_id": old_client_id,
            "source_instance": "EVE - Old Pilot",
            "system_name": "HB-FSO",
            "hostile_icon_count": 1,
            "seen_at": "2099-07-03T10:00:00+00:00",
        }
    )
    common = {
        "client_id": "detector-client:test",
        "client_type": "detector_client",
        "status": "running",
    }
    store.record_heartbeat(
        common
        | {
            "seen_at": "2099-07-03T10:00:01+00:00",
            "details": {
                "monitoring": True,
                "targets": [{"client_id": current_client_id}],
            },
        }
    )
    store.record_heartbeat(
        common
        | {
            "seen_at": "2099-07-03T10:00:02+00:00",
            "details": {
                "monitoring": True,
                "targets": [
                    {"client_id": old_client_id},
                    {"client_id": current_client_id},
                ],
            },
        }
    )

    assert store.expire_active_intel("2099-07-03T10:01:00+00:00") == 0
    assert len(store.list_active_intel(source="eve-sentry-detector")) == 1


def test_record_ocr_snapshot_moves_only_its_client_to_the_new_system(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-b",
            "source_instance": "EVE - Pilot B",
            "system_name": "S-KSWL",
            "names": ["Bob"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )

    moved = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "names": ["Carol"],
            "seen_at": "2026-07-03T10:00:10+00:00",
        }
    )

    assert moved["expired"] == 1
    assert {
        (item["source_instance"], item["system_name"], item["name"])
        for item in store.list_active_intel(source="eve-sentry-detector")
    } == {
        ("EVE - Pilot A", "HB-FSO", "Carol"),
        ("EVE - Pilot B", "S-KSWL", "Bob"),
    }
    inactive = store.list_active_intel(
        source="eve-sentry-detector",
        active=False,
    )
    assert [(item["system_name"], item["name"]) for item in inactive] == [
        ("S-KSWL", "Alice")
    ]
    assert inactive[0]["metadata"]["left_reason"] == "system_changed"
    assert inactive[0]["metadata"]["next_system_name"] == "HB-FSO"


def test_record_ocr_snapshot_ignores_delayed_previous_system_snapshot(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    client_id = "detector-client:test:pilot-a"
    store.record_ocr_snapshot(
        {
            "client_id": client_id,
            "source_instance": "EVE - Pilot A",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2026-07-03T10:00:00+00:00",
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": client_id,
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "names": ["Carol"],
            "seen_at": "2026-07-03T10:00:10+00:00",
        }
    )

    delayed = store.record_ocr_snapshot(
        {
            "client_id": client_id,
            "source_instance": "EVE - Pilot A",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2026-07-03T10:00:05+00:00",
        }
    )

    assert delayed["created"] == 0
    assert [
        (item["system_name"], item["name"])
        for item in store.list_active_intel(source="eve-sentry-detector")
    ] == [("HB-FSO", "Carol")]


def test_stale_detector_heartbeat_expires_ocr_active_intel_on_read(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2026-01-01T00:00:00+00:00",
        }
    )
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2026-01-01T00:00:01+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {"monitoring": True},
        }
    )

    assert store.list_active_intel(source="eve-sentry-detector") == []
    inactive = store.list_active_intel(source="eve-sentry-detector", active=False)
    assert inactive[0]["source_instance"] == "EVE - Pilot"
    assert inactive[0]["left_at"]


def test_stale_detector_heartbeat_expires_snapshot_seen_before_stale_deadline(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2026-01-01T00:00:01+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {"monitoring": True},
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2026-01-01T00:00:10+00:00",
        }
    )

    assert store.list_active_intel(source="eve-sentry-detector") == []
    inactive = store.list_active_intel(source="eve-sentry-detector", active=False)
    assert inactive[0]["left_at"] == "2026-01-01T00:00:16+00:00"


def test_stale_detector_heartbeat_does_not_expire_snapshot_after_stale_deadline(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2099-01-01T00:00:01+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {"monitoring": True},
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2099-01-01T00:00:30+00:00",
        }
    )

    active = store.list_active_intel(source="eve-sentry-detector")

    assert len(active) == 1
    assert active[0]["source_instance"] == "EVE - Pilot"


def test_stale_detector_heartbeat_expires_snapshot_after_stale_deadline_grace(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "running",
            "seen_at": "2026-01-01T00:00:01+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {"monitoring": True},
        }
    )
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "seen_at": "2026-01-01T00:00:30+00:00",
        }
    )

    assert store.list_active_intel(source="eve-sentry-detector") == []
    inactive = store.list_active_intel(source="eve-sentry-detector", active=False)
    assert inactive[0]["left_at"] == "2026-01-01T00:00:36+00:00"


def test_add_observation_deduplicates_same_source_time_and_raw_text(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])
    payload = {
        "source": "intel_channel",
        "source_instance": "Alliance Intel",
        "system_name": "Tama",
        "names": [],
        "raw_text": "Scout A: Tama +3 reds",
        "metadata": {"hostile_count": 3, "sender": "Scout A"},
        "seen_at": "2026-06-29T12:00:00+00:00",
    }

    first = store.add_observation(payload)
    second = store.add_observation({**payload, "id": "different-id"})
    distinct = store.add_observation(
        {
            **payload,
            "id": "another-id",
            "raw_text": "Scout A: Tama +4 reds",
            "metadata": {"hostile_count": 4, "sender": "Scout A"},
        }
    )

    assert second.observation_id == first.observation_id
    assert distinct.observation_id == "another-id"
    assert len(store.list_observations()) == 2
    assert len(store.list_alerts()) == 2

    reloaded = IntelStore(path, systems={}, links=[])
    assert len(reloaded.list_observations()) == 2


def test_ack_alert_marks_alert_and_persists(tmp_path):
    path = tmp_path / "intel_reports.json"
    store = IntelStore(path, systems={}, links=[])
    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )
    alert_id = f"evt_{observation.observation_id}"

    acked = store.ack_alert(alert_id, acknowledged_by="tester", note="handled")

    assert acked is not None
    assert acked["id"] == alert_id
    assert acked["acknowledged"] is True
    assert acked["acknowledged_at"]
    assert acked["acknowledged_by"] == "tester"
    assert acked["acknowledgement_note"] == "handled"
    assert store.ack_alert("missing") is None

    reloaded = IntelStore(path, systems={}, links=[])
    alert = reloaded.list_alerts()[0]

    assert alert["acknowledged"] is True
    assert alert["acknowledged_at"] == acked["acknowledged_at"]
    assert alert["acknowledged_by"] == "tester"
    assert alert["acknowledgement_note"] == "handled"


def test_add_observation_uses_optional_resolver(tmp_path):
    class FakeResolver:
        def enrich_observation(self, observation):
            observation.system_id = 30002813
            observation.character_ids = [123]
            return observation

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=FakeResolver(),
    )

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
        }
    )

    assert observation.system_id == 30002813
    assert observation.character_ids == [123]
    assert store.list_observations()[0]["character_ids"] == [123]


def test_add_observation_repairs_invalid_channel_system_with_resolver(tmp_path):
    class FakeResolver:
        def resolve_names(self, names):
            assert names == ["Alice", "Tama"]
            return [
                SimpleNamespace(
                    name="Alice",
                    category="character",
                    entity_id=123,
                ),
                SimpleNamespace(
                    name="Tama",
                    category="solar_system",
                    entity_id=30002813,
                ),
            ]

        def enrich_observation(self, observation):
            observation.system_id = 30002813
            observation.character_ids = [123]
            return observation

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=FakeResolver(),
    )

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Alice",
            "raw_text": "Scout A: Alice reds Tama",
            "metadata": {"sender": "Scout A", "hostile_count": 1},
        }
    )

    assert observation.system_name == "Tama"
    assert observation.system_id == 30002813
    assert observation.names == ["Alice"]
    assert observation.character_ids == [123]
    assert observation.metadata["esi_resolution"] == {
        "candidate_system_names": ["Alice", "Tama"],
        "resolved_system_candidates": ["Tama"],
        "system_repair_status": "repaired",
        "system_repaired_from": "Alice",
        "system_repaired_to": "Tama",
    }
    assert store.list_observations()[0]["system_name"] == "Tama"


def test_add_observation_keeps_ambiguous_system_candidates_in_metadata(tmp_path):
    class AmbiguousRepairClient:
        def resolve_ids(self, names):
            if names == ["Alice", "Tama", "Oijanen"]:
                return {
                    "characters": [{"id": 123, "name": "Alice"}],
                    "systems": [
                        {"id": 30002813, "name": "Tama"},
                        {"id": 30002814, "name": "Oijanen"},
                    ],
                }
            if names == ["Alice"]:
                return {
                    "characters": [{"id": 123, "name": "Alice"}],
                }
            raise AssertionError(names)

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        resolver=EsiResolver(
            client=AmbiguousRepairClient(),
            cache=EsiCache(tmp_path / "esi.json"),
        ),
        scorer=ScoringEngine(cooldown_seconds=0),
    )

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Alice",
            "raw_text": "Scout A: Alice reds Tama Oijanen",
            "metadata": {"sender": "Scout A", "hostile_count": 1},
        }
    )
    resolution = observation.metadata["esi_resolution"]

    assert observation.system_name == "Alice"
    assert observation.system_id is None
    assert observation.character_ids == [123]
    assert resolution == {
        "attempted": True,
        "candidate_system_names": ["Alice", "Tama", "Oijanen"],
        "character_name_count": 0,
        "resolved_character_count": 0,
        "resolved_system_candidates": ["Tama", "Oijanen"],
        "system_name_matched": False,
        "system_repair_status": "ambiguous",
    }
    assert store.list_alerts() == []


def test_list_alerts_uses_optional_scorer_and_caches_result(tmp_path):
    class FakeScorer:
        def __init__(self):
            self.calls = 0

        def score(self, observation):
            self.calls += 1
            if self.calls > 1:
                return None
            return ThreatEvent(
                event_id=f"custom_{observation.observation_id}",
                system_name=observation.system_name,
                names=observation.names,
                score=99,
                level="high",
                evidence=[Evidence("custom", 99, "custom score")],
                source_observation_id=observation.observation_id,
                created_at=observation.received_at,
            )

    scorer = FakeScorer()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=scorer,
    )
    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    first_alerts = store.list_alerts()
    second_alerts = store.list_alerts()

    assert first_alerts == second_alerts
    assert first_alerts[0]["id"] == f"custom_{observation.observation_id}"
    assert first_alerts[0]["score"] == 99
    assert first_alerts[0]["evidence"][0]["type"] == "custom"
    assert scorer.calls == 1
    assert store.ack_alert(first_alerts[0]["id"])["acknowledged"] is True


def test_snapshot_does_not_hold_store_lock_while_scoring(tmp_path):
    class BlockingScorer:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def score(self, observation, **kwargs):
            self.entered.set()
            self.release.wait(timeout=2)
            return ThreatEvent.from_observation(observation)

    scorer = BlockingScorer()
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=scorer,
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    snapshot_done = threading.Event()
    snapshot_errors = []

    def build_snapshot():
        try:
            store.snapshot()
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            snapshot_errors.append(exc)
        finally:
            snapshot_done.set()

    snapshot_thread = threading.Thread(target=build_snapshot, daemon=True)
    snapshot_thread.start()

    try:
        assert scorer.entered.wait(timeout=1)

        read_done = threading.Event()

        def read_reports():
            store._reports_snapshot()
            read_done.set()

        reader = threading.Thread(target=read_reports, daemon=True)
        reader.start()

        assert read_done.wait(timeout=0.2)
    finally:
        scorer.release.set()
        snapshot_thread.join(timeout=3)

    assert snapshot_done.is_set()
    assert snapshot_errors == []


def test_list_alerts_scores_with_optional_enricher(tmp_path):
    class FakeEnricher:
        def enrich(self, observation):
            return ThreatEnrichment(
                character_profiles=[
                    {"character_id": observation.character_ids[0], "corporation_id": 42}
                ],
                kill_activities=[
                    SimpleNamespace(
                        character_id=observation.character_ids[0],
                        window="7d",
                        kills=5,
                    )
                ],
            )

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_corporation_ids={42}),
            cooldown_seconds=0,
        ),
        enricher=FakeEnricher(),
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "character_ids": [123],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    alert = store.list_alerts()[0]

    assert alert["score"] == 90
    assert alert["level"] == "high"
    assert [item["type"] for item in alert["evidence"]] == [
        "intel_channel_report",
        "hostile_corporation",
    ]


def test_list_alerts_ignores_group_kill_activity_from_enricher(tmp_path):
    class FakeEnricher:
        def enrich(self, observation):
            return ThreatEnrichment(
                group_activities=[
                    SimpleNamespace(
                        entity_type="alliance",
                        entity_id=789,
                        window="7d",
                        kills=10,
                    )
                ],
            )

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
        enricher=FakeEnricher(),
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "character_ids": [123],
        }
    )

    alert = store.list_alerts()[0]

    assert alert["score"] == 30
    assert [item["type"] for item in alert["evidence"]] == ["intel_channel_report"]


def test_list_alerts_does_not_promote_ocr_context_without_hostile_evidence(tmp_path):
    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[("Tama", "Oijanen")],
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": "Tama",
            "raw_text": "Scout A: Tama +3 reds",
            "seen_at": "2026-06-29T11:58:00+00:00",
        }
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": "Oijanen",
            "raw_text": "Scout B: Oijanen Some Pilot",
            "seen_at": "2026-06-29T11:40:00+00:00",
        }
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "system_name": "Hek",
            "raw_text": "Scout C: Hek +1",
            "seen_at": "2026-06-29T10:00:00+00:00",
        }
    )
    observation = store.add_observation(
        {
            "source": "local_ocr",
            "system_name": "Tama",
            "names": ["Alice"],
            "seen_at": "2026-06-29T12:00:00+00:00",
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    alerts = store.list_alerts()
    assert not any(
        item["source_observation_id"] == observation.observation_id for item in alerts
    )


def test_enricher_failure_falls_back_to_base_scoring(tmp_path):
    class FailingEnricher:
        def enrich(self, observation):
            raise RuntimeError("offline")

    store = IntelStore(
        tmp_path / "intel_reports.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
        enricher=FailingEnricher(),
    )
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "character_ids": [123],
        }
    )

    alert = store.list_alerts()[0]

    assert alert["score"] == 30
    assert [item["type"] for item in alert["evidence"]] == [
        "intel_channel_report"
    ]
