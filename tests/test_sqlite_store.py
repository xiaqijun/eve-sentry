from app.server.intel_store import IntelStore
from app.server.sqlite_store import SQLiteIntelStore


def test_sqlite_store_persists_observations(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])

    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "character_ids": [123],
            "metadata": {"hostile_count": 1, "sender": "Scout A"},
            "seen_at": "2026-06-29T12:00:00+00:00",
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    alerts = reloaded.list_alerts()

    assert reloaded.list_observations()[0]["id"] == observation.observation_id
    assert reloaded.list_observations()[0]["metadata"]["sender"] == "Scout A"
    assert alerts[0]["source_observation_id"] == observation.observation_id
    assert alerts[0]["character_ids"] == [123]


def test_sqlite_store_deduplicates_observations(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    payload = {
        "source": "intel_channel",
        "source_instance": "Alliance Intel",
        "system_name": "Tama",
        "raw_text": "Scout A: Tama +3 reds",
        "seen_at": "2026-06-29T12:00:00+00:00",
    }

    first = store.add_observation(payload)
    second = store.add_observation({**payload, "id": "different-id"})

    assert second.observation_id == first.observation_id
    assert len(store.list_observations()) == 1

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    assert len(reloaded.list_observations()) == 1


def test_sqlite_store_persists_alert_acknowledgement(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    observation = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "received_at": "2026-06-29T12:00:01+00:00",
        }
    )
    alert_id = f"evt_{observation.observation_id}"

    acked = store.ack_alert(alert_id, acknowledged_by="client", note="sent")

    assert acked is not None
    assert acked["acknowledged"] is True
    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])

    alert = reloaded.list_alerts()[0]

    assert alert["acknowledged"] is True
    assert alert["acknowledged_at"] == acked["acknowledged_at"]
    assert alert["acknowledged_by"] == "client"
    assert alert["acknowledgement_note"] == "sent"


def test_sqlite_store_imports_legacy_json_once(tmp_path):
    json_path = tmp_path / "intel_reports.json"
    db_path = tmp_path / "intel.sqlite3"
    legacy = IntelStore(json_path, systems={}, links=[])
    report = legacy.add_report(
        "Tama",
        ["Alice"],
        source="ocr",
        seen_at="2026-06-29T12:00:00+00:00",
    )

    imported = SQLiteIntelStore(
        db_path,
        import_json_path=json_path,
        systems={},
        links=[],
    )
    imported.delete_report(report.report_id)

    reloaded = SQLiteIntelStore(
        db_path,
        import_json_path=json_path,
        systems={},
        links=[],
    )

    assert imported.list_reports() == []
    assert reloaded.list_reports() == []
