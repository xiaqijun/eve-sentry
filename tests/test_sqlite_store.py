import sqlite3

from app.server.intel_store import IntelStore
from app.server.sqlite_store import SQLiteIntelStore


def forbid_report_deletes(db_path):
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TRIGGER forbid_report_deletes
            BEFORE DELETE ON intel_reports
            BEGIN
                SELECT RAISE(ABORT, 'unexpected report delete');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()


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


def test_sqlite_store_persists_active_intel(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    active = reloaded.list_active_intel()

    assert len(active) == 1
    assert active[0]["name"] == "Alice"
    assert active[0]["active"] is True


def test_sqlite_store_persists_detector_heartbeat_active_cleanup(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )

    store.record_heartbeat(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "status": "idle",
            "seen_at": "2026-07-03T10:00:05+00:00",
            "details": {"monitoring": False, "last_action": "monitor_stopped"},
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    inactive = reloaded.list_active_intel(source="eve-sentry-detector", active=False)

    assert reloaded.list_active_intel(source="eve-sentry-detector") == []
    assert inactive[0]["left_at"] == "2026-07-03T10:00:05+00:00"


def test_sqlite_store_persists_stale_detector_active_cleanup_on_read(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-01-01T00:00:00+00:00",
            "names": ["Alice"],
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
    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    inactive = reloaded.list_active_intel(source="eve-sentry-detector", active=False)

    assert reloaded.list_active_intel(source="eve-sentry-detector") == []
    assert inactive[0]["source_instance"] == "EVE - Hajimi6"


def test_sqlite_store_persists_expired_active_intel(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
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

    store.expire_active_intel("2026-07-03T10:03:01+00:00")
    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    inactive = reloaded.list_active_intel(source="intel_channel", active=False)

    assert reloaded.list_active_intel(source="intel_channel") == []
    assert inactive[0]["left_at"] == "2026-07-03T10:03:01+00:00"


def test_sqlite_store_migrates_legacy_active_intel_table(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE active_intel (
                active_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_instance TEXT NOT NULL,
                system TEXT NOT NULL,
                target_type TEXT NOT NULL,
                name TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = SQLiteIntelStore(db_path, systems={}, links=[])
    connection = sqlite3.connect(db_path)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(active_intel)")
        }
    finally:
        connection.close()

    assert store.list_active_intel() == []
    assert {
        "system_id",
        "character_id",
        "expires_at",
        "left_at",
        "cleared_at",
        "active",
        "seen_count",
        "confidence",
        "source_observation_ids_json",
    } <= columns


def test_sqlite_store_skips_dirty_active_intel_rows(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    SQLiteIntelStore(db_path, systems={}, links=[])
    connection = sqlite3.connect(db_path)
    valid_row = (
        "valid-active-row",
        "eve-sentry-detector",
        "EVE - Hajimi6",
        "S-KSWL",
        "character",
        "Alice",
        "Alice",
        '{"client_id": "detector-client:test"}',
        "2026-07-03T10:00:00+00:00",
        "2026-07-03T10:00:00+00:00",
        1,
        2,
        0.95,
        '["obs-valid"]',
    )
    dirty_rows = [
        ("dirty-metadata", "[]", 1, 1, 0.9, '["obs-1"]'),
        ("dirty-source-ids", "{}", 1, 1, 0.9, '{"obs": "not-list"}'),
        ("dirty-active-text", "{}", "bad-active", 1, 0.9, '["obs-1"]'),
        ("dirty-active-number", "{}", 2, 1, 0.9, '["obs-1"]'),
        ("dirty-active-float", "{}", 1.5, 1, 0.9, '["obs-1"]'),
        ("dirty-seen-zero", "{}", 1, 0, 0.9, '["obs-1"]'),
        ("dirty-seen-text", "{}", 1, "bad", 0.9, '["obs-1"]'),
        ("dirty-seen-float", "{}", 1, 1.5, 0.9, '["obs-1"]'),
        ("dirty-confidence", "{}", 1, 1, "bad-confidence", '["obs-1"]'),
    ]
    try:
        connection.executemany(
            """
            INSERT INTO active_intel (
                active_id, source, source_instance, system, target_type, name,
                raw_text, metadata_json, first_seen_at, last_seen_at, active,
                seen_count, confidence, source_observation_ids_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [valid_row]
            + [
                (
                    active_id,
                    "eve-sentry-detector",
                    "EVE - Hajimi6",
                    "S-KSWL",
                    "character",
                    "Bad Row",
                    "Bad Row",
                    metadata_json,
                    "2026-07-03T10:00:00+00:00",
                    "2026-07-03T10:00:00+00:00",
                    active,
                    seen_count,
                    confidence,
                    source_ids_json,
                )
                for (
                    active_id,
                    metadata_json,
                    active,
                    seen_count,
                    confidence,
                    source_ids_json,
                ) in dirty_rows
            ],
        )
        connection.commit()
    finally:
        connection.close()

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    active = reloaded.list_active_intel()

    assert len(active) == 1
    assert active[0]["id"] == "valid-active-row"
    assert active[0]["metadata"] == {"client_id": "detector-client:test"}
    assert active[0]["source_observation_ids"] == ["obs-valid"]
    assert active[0]["active"] is True
    assert active[0]["seen_count"] == 2
    assert active[0]["confidence"] == 0.95


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


def test_sqlite_store_add_observation_does_not_rewrite_existing_reports(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "seen_at": "2026-06-29T12:00:00+00:00",
        }
    )
    forbid_report_deletes(db_path)

    created = store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Kedama",
            "names": ["Bob"],
            "seen_at": "2026-06-29T12:01:00+00:00",
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    assert {item["id"] for item in reloaded.list_observations()} >= {
        created.observation_id
    }


def test_sqlite_store_ack_alert_does_not_rewrite_reports(tmp_path):
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
    forbid_report_deletes(db_path)

    acked = store.ack_alert(
        f"evt_{observation.observation_id}",
        acknowledged_by="client",
        note="sent",
    )

    assert acked is not None
    assert acked["acknowledged"] is True


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


def test_sqlite_store_persists_heartbeats_across_reload(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])

    created = store.record_heartbeat(
        {
            "client_id": "detector:test",
            "client_type": "detector_client",
            "label": "Detector Client",
            "status": "running",
            "seen_at": "2026-07-01T10:00:00+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {
                "monitoring": True,
                "system": "Tama",
            },
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    payload = reloaded.heartbeat_snapshot()

    assert created["client_id"] == "detector:test"
    assert payload["count"] == 1
    assert payload["summary"]["by_type"] == {"detector_client": 1}
    assert payload["summary"]["by_status"] == {"running": 1}
    assert payload["heartbeats"][0]["client_id"] == "detector:test"
    assert payload["heartbeats"][0]["details"]["system"] == "Tama"


def test_sqlite_store_updates_existing_heartbeat_by_client_id(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    store = SQLiteIntelStore(db_path, systems={}, links=[])

    store.record_heartbeat(
        {
            "client_id": "alert:test",
            "client_type": "alert_client",
            "label": "Alert Client",
            "status": "idle",
            "seen_at": "2026-07-01T10:00:00+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {"transport": "poll"},
        }
    )
    store.record_heartbeat(
        {
            "client_id": "alert:test",
            "client_type": "alert_client",
            "label": "Alert Client",
            "status": "running",
            "seen_at": "2026-07-01T10:00:05+00:00",
            "heartbeat_interval_seconds": 5,
            "details": {"transport": "stream"},
        }
    )

    reloaded = SQLiteIntelStore(db_path, systems={}, links=[])
    heartbeats = reloaded.list_heartbeats()

    assert len(heartbeats) == 1
    assert heartbeats[0]["status"] == "running"
    assert heartbeats[0]["details"]["transport"] == "stream"
