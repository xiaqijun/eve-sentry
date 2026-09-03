import json
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from app.core.active_intel import ActiveIntelItem
from app.core.models import Evidence, ThreatEvent
from app.intel.classification import ClassificationEngine
from app.intel.scoring import Watchlist
from app.server.intel_store import IntelReport, IntelStore
from app.server.postgres_store import (
    POSTGRES_POOL_MAX_SIZE,
    POSTGRES_POOL_MIN_SIZE,
    POSTGRES_POOL_TIMEOUT_SECONDS,
    PERSISTED_ALERT_METADATA_KEY,
    REPORT_STREAM_ADVISORY_LOCK_ID,
    REPORT_STREAM_POSITION_KEY,
    PostgreSQLIntelStore,
    _PostgresConnection,
    _create_connection_pool,
    _redact_dsn,
)


def test_postgres_store_requires_dsn():
    with pytest.raises(ValueError, match="postgres dsn is required"):
        PostgreSQLIntelStore("")


def test_postgres_dsn_redaction_hides_credentials():
    redacted = _redact_dsn(
        "postgresql://eve_sentry:super-secret@db.internal:5432/eve_sentry"
    )

    assert redacted == "postgresql://***@db.internal:5432/eve_sentry"
    assert "super-secret" not in redacted


def test_postgres_pool_uses_bounded_reusable_connections(monkeypatch):
    captured = {}
    dict_row = object()

    class FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    psycopg_module = types.ModuleType("psycopg")
    rows_module = types.ModuleType("psycopg.rows")
    rows_module.dict_row = dict_row
    pool_module = types.ModuleType("psycopg_pool")
    pool_module.ConnectionPool = FakePool
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_module)

    pool = _create_connection_pool("postgresql://example/eve_sentry")

    assert isinstance(pool, FakePool)
    assert captured == {
        "conninfo": "postgresql://example/eve_sentry",
        "min_size": POSTGRES_POOL_MIN_SIZE,
        "max_size": POSTGRES_POOL_MAX_SIZE,
        "timeout": POSTGRES_POOL_TIMEOUT_SECONDS,
        "kwargs": {"row_factory": dict_row},
        "open": True,
    }


def test_postgres_connection_returns_connection_to_pool_context():
    calls = []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((query, params))
            return "result"

    class FakePoolContext:
        def __enter__(self):
            calls.append("acquire")
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            calls.append("release")
            return False

    connection = _PostgresConnection(FakePoolContext())

    with connection as active:
        assert active.execute("SELECT ?", (1,)) == "result"

    assert calls == ["acquire", ("SELECT %s", (1,)), "release"]
    with pytest.raises(RuntimeError, match="not active"):
        connection.execute("SELECT 1")


def test_postgres_connection_converts_bulk_delete_placeholders():
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def executemany(self, query, params):
            calls.append((query, params))

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    connection = _PostgresConnection(FakePoolContext())
    params = [("old-1",), ("old-2",)]

    with connection as active:
        active.executemany(
            "DELETE FROM intel_reports WHERE report_id = ?",
            params,
        )

    assert calls == [
        ("DELETE FROM intel_reports WHERE report_id = %s", params)
    ]


def test_postgres_migration_adds_heartbeat_attribution_columns():
    queries = []

    class EmptyResult:
        def fetchone(self):
            return None

    class FakeConnection:
        def execute(self, query, params=None):
            queries.append((" ".join(query.split()), params))
            return EmptyResult()

    class FakeContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: FakeContext()

    store._migrate()

    migration_sql = "\n".join(query for query, _params in queries)
    lock_query_index = next(
        index
        for index, (query, _params) in enumerate(queries)
        if "pg_advisory_xact_lock" in query
    )
    sequence_query_index = next(
        index
        for index, (query, _params) in enumerate(queries)
        if "CREATE SEQUENCE" in query
    )
    assert lock_query_index < sequence_query_index
    assert "CREATE SEQUENCE IF NOT EXISTS intel_reports_stream_position_seq" in (
        migration_sql
    )
    assert "ALTER TABLE intel_reports ADD COLUMN stream_position BIGINT" in migration_sql
    assert "ALTER COLUMN stream_position SET DEFAULT nextval(" in migration_sql
    assert "ORDER BY received_at ASC, report_id ASC" in migration_sql
    assert "ALTER COLUMN stream_position SET NOT NULL" in migration_sql
    assert "OWNED BY intel_reports.stream_position" in migration_sql
    assert "SELECT setval(" in migration_sql
    assert "IS DISTINCT FROM to_jsonb(stream_position)" in migration_sql
    assert "idx_intel_reports_stream_position" in migration_sql
    assert "ALTER TABLE client_heartbeats ADD COLUMN user_id" in migration_sql
    assert "ALTER TABLE client_heartbeats ADD COLUMN api_key_id" in migration_sql
    assert "ALTER TABLE client_heartbeats ADD COLUMN remote_ip" in migration_sql


def test_postgres_heartbeat_row_accepts_legacy_empty_attribution():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    heartbeat = store._heartbeat_from_row(
        {
            "client_id": "legacy-client",
            "client_type": "detector_client",
            "label": "Legacy",
            "status": "running",
            "seen_at": "2026-08-04T00:00:00+00:00",
            "heartbeat_interval_seconds": 15,
            "details_json": "{}",
            "user_id": None,
            "api_key_id": None,
            "remote_ip": None,
        }
    )

    assert heartbeat is not None
    assert heartbeat["user_id"] == ""
    assert heartbeat["api_key_id"] == ""
    assert heartbeat["remote_ip"] == ""
    assert store._heartbeat_row(heartbeat)[-3:] == ("", "", "")


def test_postgres_heartbeat_roundtrip_keeps_private_detector_state_out_of_view():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    heartbeat = store._heartbeat_from_row(
        {
            "client_id": "detector-client:test",
            "client_type": "detector_client",
            "label": "Detector",
            "status": "idle",
            "seen_at": "2099-07-03T10:00:06+00:00",
            "heartbeat_interval_seconds": 5,
            "details_json": (
                '{"monitoring":false,"last_action":"ocr_snapshot:1",'
                '"_server_monitor_stopped_at":"2099-07-03T10:00:01+00:00",'
                '"_server_monitor_target_ids":["detector-client:test:user-200"],'
                '"_server_missing_targets":{'
                '"detector-client:test:user-100":"2099-07-03T10:00:01+00:00"}}'
            ),
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "127.0.0.1",
        }
    )

    assert heartbeat is not None
    assert heartbeat["details"]["_server_monitor_stopped_at"] == (
        "2099-07-03T10:00:01+00:00"
    )
    persisted_details = json.loads(store._heartbeat_row(heartbeat)[6])
    assert persisted_details["_server_monitor_stopped_at"] == (
        "2099-07-03T10:00:01+00:00"
    )
    assert persisted_details["_server_monitor_target_ids"] == [
        "detector-client:test:user-200"
    ]
    assert persisted_details["_server_missing_targets"] == {
        "detector-client:test:user-100": "2099-07-03T10:00:01+00:00"
    }
    assert store._heartbeat_view(heartbeat)["details"] == {
        "monitoring": False,
        "last_action": "ocr_snapshot:1",
    }


def test_postgres_refresh_detector_heartbeat_persists_parent(monkeypatch):
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._lock = threading.RLock()
    store._heartbeats = {
        "detector:parent": {
            "client_id": "detector:parent",
            "client_type": "detector_client",
            "label": "Detector",
            "status": "running",
            "seen_at": "2026-08-28T00:00:00+00:00",
            "heartbeat_interval_seconds": 15,
            "details": {
                "targets": [{"client_id": "detector:window-a"}],
            },
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "203.0.113.9",
        }
    }
    persisted = []
    store._write_heartbeat = lambda heartbeat: persisted.append(heartbeat)
    monkeypatch.setattr(
        "app.server.intel_store.utc_now_iso",
        lambda: "2026-08-28T00:00:30+00:00",
    )

    refreshed = store.refresh_detector_heartbeat("detector:window-a")

    assert refreshed is not None
    assert refreshed["client_id"] == "detector:parent"
    assert persisted == [store._heartbeats["detector:parent"]]
    assert persisted[0]["seen_at"] == "2026-08-28T00:00:30+00:00"
    assert persisted[0]["details"] == {
        "targets": [{"client_id": "detector:window-a"}],
    }


def test_postgres_heartbeat_load_prunes_only_attributed_logical_duplicates():
    rows = [
        {
            "client_id": "detector-client:new",
            "client_type": "detector_client",
            "label": "Detector",
            "status": "running",
            "seen_at": "2026-08-10T10:00:00+00:00",
            "heartbeat_interval_seconds": 15,
            "details_json": '{"host":"Scout-PC"}',
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "127.0.0.1",
        },
        {
            "client_id": "detector-client:old",
            "client_type": "detector_client",
            "label": "Detector",
            "status": "running",
            "seen_at": "2026-08-09T10:00:00+00:00",
            "heartbeat_interval_seconds": 15,
            "details_json": '{"host":"scout-pc"}',
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "127.0.0.1",
        },
        {
            "client_id": "legacy-without-host",
            "client_type": "detector_client",
            "label": "Legacy",
            "status": "idle",
            "seen_at": "2026-08-08T10:00:00+00:00",
            "heartbeat_interval_seconds": 15,
            "details_json": "{}",
            "user_id": "user-1",
            "api_key_id": "key-1",
            "remote_ip": "127.0.0.1",
        },
    ]
    deleted = []

    class Result:
        def fetchall(self):
            return rows

    class FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query, params=None):
            assert "FROM client_heartbeats" in query
            return Result()

        def executemany(self, query, params_seq):
            assert "DELETE FROM client_heartbeats" in query
            deleted.extend(params_seq)

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = FakeContext

    heartbeats = store._read_heartbeats()

    assert set(heartbeats) == {
        "detector-client:new",
        "legacy-without-host",
    }
    assert deleted == [("detector-client:old",)]


@pytest.mark.parametrize("method_name", ["list_reports", "list_observations"])
def test_postgres_limited_history_uses_keyset_query(method_name):
    report = IntelReport(
        report_id="report-1",
        system="Tama",
        names=["Pilot"],
        source="manual",
    )
    page_calls = []
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._report_page_items = lambda **kwargs: (
        page_calls.append(kwargs) or ([report], "next")
    )
    store._read_reports = lambda **_kwargs: pytest.fail(
        "limited history must not read the full report table"
    )

    result = getattr(store, method_name)(limit=1)

    assert len(result) == 1
    assert page_calls[0]["limit"] == 1


@pytest.mark.parametrize(
    ("kwargs", "expected_clause", "expected_params"),
    [
        (
            {"character_id": 9001},
            "character_ids_json::jsonb @> %s::jsonb",
            ('[9001]',),
        ),
        ({"system_id": 30000142}, "system_id = %s", (30000142,)),
    ],
)
def test_postgres_entity_report_queries_filter_in_database(
    kwargs,
    expected_clause,
    expected_params,
):
    calls = []

    class EmptyResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return EmptyResult()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    assert store._read_reports(**kwargs) == []
    assert expected_clause in calls[0][0]
    assert calls[0][1] == expected_params


@pytest.mark.parametrize(
    ("returned_row", "deleted"),
    [(None, False), ({"report_id": "report-1"}, True)],
)
def test_postgres_delete_report_respects_active_database_references(
    returned_row,
    deleted,
):
    calls = []

    class Result:
        def fetchone(self):
            return returned_row

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    assert store._delete_report("report-1") is deleted
    query, params = calls[0]
    assert "NOT EXISTS" in query
    assert "jsonb_exists" in query
    assert "active = 1" in query
    assert "RETURNING report.report_id" in query
    assert params == ("report-1",)


def test_postgres_report_page_query_uses_converted_keyset_placeholders():
    calls = []

    class EmptyResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((query, params))
            return EmptyResult()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    rows = store._read_report_page_rows(
        anchor=("seen", "received", "report-id"),
        limit=25,
        source="manual",
        system="tama",
    )

    assert rows == []
    query, params = calls[0]
    assert "?" not in query
    assert query.count("%s") == 9
    assert params == (
        "manual",
        "tama",
        "seen",
        "seen",
        "received",
        "seen",
        "received",
        "report-id",
        25,
    )


def test_postgres_alert_history_uses_bounded_database_page():
    report = IntelReport(
        report_id="report-1",
        system="Tama",
        names=["Pilot"],
        source="eve-sentry-detector",
        seen_at="2026-08-03T10:00:00+00:00",
        received_at="2026-08-03T10:00:01+00:00",
    )
    alert = SimpleNamespace()
    page_calls = []
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._report_page_items = lambda **_kwargs: pytest.fail(
        "alert history must not use the in-memory report page"
    )
    store._reports_snapshot = lambda: pytest.fail(
        "alert history must not scan the startup hot set"
    )
    store._read_alert_report_rows = lambda **kwargs: (
        page_calls.append(kwargs) or [{
            "report_id": report.report_id,
            "received_at": report.received_at,
        }]
    )
    store._report_from_row = lambda _row: report
    store._alert_from_persisted_report = lambda _report: alert
    store._alert_to_dict = lambda _report, _alert: {
        "id": "evt_report-1",
        "score": 100,
        "level": "critical",
        "acknowledged": False,
    }

    result = store.list_alert_history(
        since="2026-08-03T00:00:00+00:00",
        limit=1,
    )

    assert result == [{
        "id": "evt_report-1",
        "score": 100,
        "level": "critical",
        "acknowledged": False,
    }]
    assert page_calls == [{
        "anchor": None,
        "since": "2026-08-03T00:00:00+00:00",
        "include_since": False,
        "limit": 100,
    }]


def test_postgres_alert_history_rejects_invalid_since():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._read_alert_report_rows = lambda **_kwargs: pytest.fail(
        "invalid timestamps must fail before querying PostgreSQL"
    )

    with pytest.raises(ValueError, match="since must be an ISO timestamp"):
        store.list_alert_history(since="not-a-timestamp", limit=25)


def test_postgres_alert_history_continues_after_filtered_database_page():
    report = IntelReport(
        report_id="report-found",
        system="Tama",
        names=["Pilot"],
        received_at="2026-08-03T09:59:00+00:00",
    )
    first_page = [
        {
            "report_id": f"report-filtered-{index:03d}",
            "received_at": "2026-08-03T10:00:00+00:00",
        }
        for index in range(100)
    ]
    second_page = [
        {"report_id": report.report_id, "received_at": report.received_at}
    ]
    pages = [first_page, second_page]
    calls = []
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._read_alert_report_rows = lambda **kwargs: (
        calls.append(kwargs) or pages.pop(0)
    )
    store._report_from_row = lambda row: (
        report if row["report_id"] == report.report_id else None
    )
    store._alert_from_persisted_report = lambda _report: SimpleNamespace()
    store._alert_to_dict = lambda _report, _alert: {
        "id": "evt_report-found",
        "score": 100,
        "level": "critical",
        "acknowledged": False,
    }

    result = store.list_alert_history(limit=1)

    assert [item["id"] for item in result] == ["evt_report-found"]
    assert len(calls) == 2
    assert calls[1]["anchor"] == (
        "2026-08-03T10:00:00+00:00",
        "report-filtered-099",
    )


def test_postgres_persisted_alert_scoring_does_not_use_live_enrichment():
    report = IntelReport(
        report_id="report-1",
        system="Tama",
        names=["Pilot"],
        source="eve-sentry-detector",
        metadata={
            "character_profiles": [
                {"character_id": 9001, "corporation_id": 42}
            ]
        },
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._lock = threading.RLock()
    store._alert_cache = {}
    store._character_profile_cache = {}
    store._resolver = None
    store._scorer = ClassificationEngine(
        watchlist=Watchlist(hostile_corporation_ids={42}),
        cooldown_seconds=60,
    )
    store._alert_from_report = lambda _report: pytest.fail(
        "persisted history must not call the live enrichment scoring path"
    )

    first = store._alert_from_persisted_report(report)
    second = store._alert_from_persisted_report(report)

    assert first is not None
    assert second is not None
    assert first.classification == "red"
    assert first.reason == "Hostile corporation id 42"
    assert store._alert_cache == {}


def test_postgres_history_drops_legacy_detector_snapshot_for_friendly_identity():
    report = IntelReport(
        report_id="report-friendly",
        system="S-KSWL",
        names=["chin harry"],
        source="eve-sentry-detector",
        character_ids=[92358740],
        metadata={
            "character_profiles": [
                {"character_id": 92358740, "contact_standing": 10.0}
            ],
            PERSISTED_ALERT_METADATA_KEY: {
                "id": "evt-report-friendly",
                "system_name": "S-KSWL",
                "names": ["chin harry"],
                "character_ids": [92358740],
                "score": 100,
                "level": "critical",
                "classification": "red",
                "evidence": [{"type": "hostile_icon", "weight": 100}],
            },
        },
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._lock = threading.RLock()
    store._resolver = None
    store._character_profile_cache = {}
    store._scorer = ClassificationEngine(cooldown_seconds=0)

    alert = store._alert_from_persisted_report(report)

    assert alert is not None
    assert alert.classification == "white"


def test_postgres_realtime_alerts_keep_shared_store_behavior():
    assert PostgreSQLIntelStore.list_alerts is IntelStore.list_alerts


def test_postgres_realtime_alert_snapshot_survives_standing_cache_loss():
    calls = []

    class Result:
        rowcount = 1

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    report = IntelReport(
        report_id="standing-report",
        system="Tama",
        names=["Standing Pilot"],
        character_ids=[9001],
        source="intel_channel",
    )
    alert = ThreatEvent(
        event_id="evt_standing-report",
        system_name="Tama",
        names=["Standing Pilot"],
        character_ids=[9001],
        score=100,
        level="critical",
        evidence=[Evidence("hostile_standing", 100, "Hostile standing -10")],
        source_observation_id=report.report_id,
        classification="red",
        reason="Hostile standing -10",
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._lock = threading.RLock()
    store._alert_cache = {report.report_id: alert}
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    realtime = store._alert_from_report(report)

    assert realtime is alert
    assert len(calls) == 1
    query, params = calls[0]
    assert "jsonb_build_object(%s::text, %s::jsonb)" in query
    assert params[0] == PERSISTED_ALERT_METADATA_KEY
    assert params[2] == report.report_id
    assert report.metadata[PERSISTED_ALERT_METADATA_KEY]["reason"] == (
        "Hostile standing -10"
    )

    store._scorer = ClassificationEngine(watchlist=Watchlist())
    restored = store._alert_from_persisted_report(report)

    assert restored is not None
    assert restored.classification == "red"
    assert restored.reason == "Hostile standing -10"
    assert restored.evidence[0].evidence_type == "hostile_standing"


def test_postgres_report_upsert_preserves_existing_alert_snapshot():
    calls = []

    class Result:
        rowcount = 1

        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

    class FakeConnection:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            if "nextval" in query:
                return Result({"stream_position": 12345})
            if "RETURNING stream_position" in query:
                return Result({"stream_position": 42})
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    report = IntelReport(
        report_id="report-upsert",
        system="Tama",
        names=["Pilot"],
        metadata={"character_profiles": [{"character_id": 9001}]},
    )

    store._upsert_report(report)

    query, params = next(
        (query, params)
        for query, params in calls
        if "INSERT INTO intel_reports" in query
    )
    assert "jsonb_strip_nulls(jsonb_build_object(" in query
    assert "-> 'generated_alert'" in query
    assert "'_stream_position', intel_reports.stream_position" in query
    assert params[0] == report.report_id
    assert report.stream_position == 42


def test_postgres_persisted_alert_scoring_uses_cache_only_profile_fallback():
    class Cache:
        def get(self, key):
            assert key == "character:9001"
            return {"corporation_id": 42}

        def get_stale(self, _key):
            pytest.fail("fresh cached profile should be preferred")

    class Resolver:
        cache = Cache()

        def character_profile(self, _character_id):
            pytest.fail("historical scoring must not call ESI")

    report = IntelReport(
        report_id="report-cache-only",
        system="Tama",
        names=["Pilot"],
        character_ids=[9001],
        source="eve-sentry-detector",
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._lock = threading.RLock()
    store._alert_cache = {report.report_id: None}
    store._character_profile_cache = {}
    store._resolver = Resolver()
    store._enricher = SimpleNamespace(
        enrich=lambda _observation: pytest.fail(
            "historical scoring must not call the enricher"
        )
    )
    store._scorer = ClassificationEngine(
        watchlist=Watchlist(hostile_corporation_ids={42}),
        cooldown_seconds=60,
    )

    alert = store._alert_from_persisted_report(report)

    assert alert is not None
    assert alert.classification == "red"
    assert alert.reason == "Hostile corporation id 42"
    assert store._alert_cache == {report.report_id: None}


def test_postgres_persisted_profiles_merge_cache_layers_with_snapshot_priority():
    class Cache:
        def get(self, _key):
            return {"character_id": 9001, "corporation_id": 1, "alliance_id": 7}

    report = IntelReport(
        report_id="report-layered-profile",
        system="Tama",
        names=["Pilot"],
        character_ids=[9001],
        metadata={
            "character_profiles": [
                {
                    "character_id": 9001,
                    "corporation_id": 42,
                    "contact_standing": -10.0,
                }
            ]
        },
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._lock = threading.RLock()
    store._character_profile_cache = {
        9001: {"character_id": 9001, "corporation_id": 2, "name": "Pilot"}
    }
    store._resolver = SimpleNamespace(cache=Cache())

    profiles = store._persisted_character_profiles(report)

    assert profiles == [
        {
            "character_id": 9001,
            "corporation_id": 42,
            "alliance_id": 7,
            "name": "Pilot",
            "contact_standing": -10.0,
        }
    ]


def test_postgres_alert_page_query_filters_and_pages_by_received_at():
    calls = []

    class EmptyResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((query, params))
            return EmptyResult()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    rows = store._read_alert_report_rows(
        anchor=("2026-08-03T10:00:00+00:00", "report-2"),
        since="2026-08-03T00:00:00+00:00",
        include_since=True,
        limit=50,
    )

    assert rows == []
    query, params = calls[0]
    assert "received_at >= %s" in query
    assert "ORDER BY received_at DESC, report_id DESC" in query
    assert query.count("%s") == 5
    assert params == (
        "2026-08-03T00:00:00+00:00",
        "2026-08-03T10:00:00+00:00",
        "2026-08-03T10:00:00+00:00",
        "report-2",
        50,
    )


def test_postgres_report_row_round_trip_hides_stream_position_metadata():
    report = IntelReport(
        report_id="report-stream",
        system="Tama",
        names=["Pilot"],
        metadata={"sender": "Scout"},
        stream_position=12345,
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    values = store._row_from_report(report)
    row = dict(
        zip(
            (
                "report_id",
                "stream_position",
                "system",
                "names_json",
                "source",
                "source_instance",
                "system_id",
                "character_ids_json",
                "confidence",
                "note",
                "raw_text",
                "metadata_json",
                "seen_at",
                "received_at",
                "acknowledged_at",
                "acknowledged_by",
                "acknowledgement_note",
            ),
            values,
        )
    )

    persisted_metadata = json.loads(row["metadata_json"])
    row["metadata_json"] = json.dumps(
        {"sender": "Scout", REPORT_STREAM_POSITION_KEY: 99999}
    )
    restored = store._report_from_row(row)

    assert persisted_metadata[REPORT_STREAM_POSITION_KEY] == 12345
    assert report.metadata == {"sender": "Scout"}
    assert restored is not None
    assert restored.stream_position == 12345
    assert restored.metadata == {"sender": "Scout"}


def test_postgres_stream_positions_are_allocated_under_transaction_lock():
    calls = []
    next_position = iter((101, 102))

    class Result:
        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

    class FakeConnection:
        def execute(self, query, params=None):
            normalized = " ".join(query.split())
            calls.append((normalized, params))
            if "nextval" in normalized:
                return Result({"stream_position": next(next_position)})
            return Result()

    reports = [
        IntelReport(system="Tama", names=["Pilot 1"]),
        IntelReport(system="Tama", names=["Pilot 2"]),
    ]
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    store._assign_report_stream_positions(FakeConnection(), reports)

    assert "pg_advisory_xact_lock" in calls[0][0]
    assert calls[0][1] == (REPORT_STREAM_ADVISORY_LOCK_ID,)
    assert all(
        "intel_reports_stream_position_seq" in query
        for query, _params in calls[1:]
    )
    assert [report.stream_position for report in reports] == [101, 102]


def test_postgres_batch_upsert_refreshes_stream_positions_from_database():
    calls = []

    class Result:
        def fetchall(self):
            return [
                {"report_id": "report-1", "stream_position": 7},
                {"report_id": "report-2", "stream_position": 11},
            ]

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    reports = [
        IntelReport(
            report_id="report-1",
            system="Tama",
            names=["Pilot 1"],
            stream_position=101,
        ),
        IntelReport(
            report_id="report-2",
            system="Tama",
            names=["Pilot 2"],
            stream_position=102,
        ),
    ]
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    store._refresh_report_stream_positions(FakeConnection(), reports)

    assert [report.stream_position for report in reports] == [7, 11]
    assert "SELECT report_id, stream_position" in calls[0][0]
    assert calls[0][1] == ("report-1", "report-2")


def test_postgres_alert_stream_query_uses_position_keyset():
    calls = []

    class EmptyResult:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, query, params):
            calls.append((query, params))
            return EmptyResult()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    rows = store._read_alert_stream_report_rows(
        anchor=(12345, "report-1"),
        since="",
        include_since=False,
        limit=50,
        descending=False,
    )

    assert rows == []
    query, params = calls[0]
    assert "stream_position" in query
    assert "ORDER BY" in query and "ASC" in query
    assert query.count("%s") == 4
    assert params == (12345, 12345, "report-1", 50)


def test_postgres_alert_stream_page_does_not_scan_hot_report_cache():
    reports = [
        IntelReport(
            report_id=f"report-{index:02d}",
            system="Tama",
            names=[f"Pilot {index:02d}"],
            stream_position=index,
        )
        for index in range(1, 52)
    ]
    rows_by_id = {
        report.report_id: {
            "report_id": report.report_id,
            "stream_position": report.stream_position,
            "metadata_json": json.dumps(
                {REPORT_STREAM_POSITION_KEY: report.stream_position}
            ),
        }
        for report in reports
    }
    reports_by_id = {report.report_id: report for report in reports}
    calls = []
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._reports_snapshot = lambda: pytest.fail(
        "stream pagination must not scan the startup hot report cache"
    )

    def read_rows(**kwargs):
        calls.append(kwargs)
        anchor = kwargs["anchor"] or (0, "")
        return [
            rows_by_id[report.report_id]
            for report in reports
            if (report.stream_position, report.report_id) > anchor
        ]

    store._read_alert_stream_report_rows = read_rows
    store._report_from_row = lambda row: reports_by_id[row["report_id"]]
    store._alert_from_persisted_report = lambda _report: SimpleNamespace()
    store._alert_to_dict = lambda report, _alert: {
        "id": f"evt_{report.report_id}",
        "score": 100,
        "level": "critical",
        "acknowledged": False,
    }

    first = store.list_alert_stream_page(after=(0, ""), limit=50)
    second = store.list_alert_stream_page(after=first[-1][0], limit=50)

    assert [cursor[0] for cursor, _ in first] == list(range(1, 51))
    assert [cursor[0] for cursor, _ in second] == [51]
    assert calls[0]["descending"] is False


def test_postgres_startup_hot_set_is_bounded_and_keeps_active_references():
    calls = []

    def report(report_id, timestamp):
        return IntelReport(
            report_id=report_id,
            system="Tama",
            names=[report_id],
            source="manual",
            seen_at=timestamp,
            received_at=timestamp,
        )

    hot_reports = [
        report("hot-new", "2026-07-30T12:02:00+00:00"),
        report("hot-old", "2026-07-30T12:01:00+00:00"),
    ]
    active_report = report("active-old", "2026-01-01T00:00:00+00:00")

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class FakeConnection:
        def execute(self, query, params):
            normalized = " ".join(query.split())
            calls.append((normalized, params))
            if "WHERE report_id IN" in normalized:
                return Result([active_report])
            return Result(hot_reports)

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._hot_report_limit = 2
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._report_from_row = lambda row: row
    store._ensure_system = lambda _system: None

    loaded = store._read_hot_reports({"active-old"})

    assert [item.report_id for item in loaded] == [
        "active-old",
        "hot-old",
        "hot-new",
    ]
    history_query, history_params = calls[0]
    assert "ORDER BY seen_at DESC, received_at DESC, report_id DESC" in history_query
    assert "LIMIT %s" in history_query
    assert history_params == (2,)
    assert "WHERE report_id IN (%s)" in calls[1][0]
    assert calls[1][1] == ("active-old",)


def test_postgres_startup_reads_only_active_intel_rows():
    calls = []
    item = SimpleNamespace(active_id="active-1", system_name="Tama")

    class Result:
        def fetchall(self):
            return [item]

    class FakeConnection:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._active_item_from_row = lambda row: row
    store._ensure_system = lambda _system: None

    loaded = store._read_active_intel()

    assert loaded == {"active-1": item}
    assert "WHERE active = 1" in calls[0][0]


def test_postgres_hostile_wave_state_uses_appearance_to_clear_lifecycle():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._active_intel = {}

    def item(active_id, name, first_seen, last_seen, active=True):
        return ActiveIntelItem(
            active_id=active_id,
            source="eve-sentry-detector",
            source_instance=active_id,
            system_name="S-KSWL",
            name=name,
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            active=active,
        )

    first = item(
        "ocr:first",
        "Alice",
        "2026-08-03T10:00:00+00:00",
        "2026-08-03T10:01:00+00:00",
    )
    second = item(
        "ocr:second",
        "Bob",
        "2026-08-03T10:00:30+00:00",
        "2026-08-03T10:01:30+00:00",
    )
    both = store._hostile_system_state([first, second])
    one = store._hostile_system_state([second])

    opened = store._hostile_wave_changes(
        {},
        "2026-08-03T10:00:00+00:00",
        after=both,
    )
    still_open = store._hostile_wave_changes(
        both,
        "2026-08-03T10:02:00+00:00",
        after=one,
    )
    cleared = store._hostile_wave_changes(
        one,
        "2026-08-03T10:03:00+00:00",
        after={},
    )
    reopened = store._hostile_wave_changes(
        {},
        "2026-08-03T10:03:30+00:00",
        after=one,
    )

    assert len(opened) == 1
    assert opened[0]["action"] == "touch"
    assert opened[0]["started_at"] == "2026-08-03T10:00:00+00:00"
    assert still_open[0]["action"] == "touch"
    assert cleared[0]["action"] == "clear"
    assert cleared[0]["cleared_at"] == "2026-08-03T10:03:00+00:00"
    assert reopened[0]["action"] == "touch"
    assert reopened[0]["started_at"] == "2026-08-03T10:03:30+00:00"


def test_postgres_hostile_wave_state_deduplicates_detector_visual_counts():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._active_intel = {}

    def item(active_id, client_id, count, seen_at):
        return ActiveIntelItem(
            active_id=active_id,
            source="eve-sentry-detector",
            source_instance=client_id,
            system_name="S-KSWL",
            target_type="system",
            metadata={
                "client_id": client_id,
                "hostile_icon_count": count,
                "hostile_icon_seen_at": seen_at,
            },
            first_seen_at="2026-08-03T10:00:00+00:00",
            last_seen_at=seen_at,
        )

    state = store._hostile_system_state(
        [
            item("client-a:old", "client-a", 2, "2026-08-03T10:00:00+00:00"),
            item("client-a:new", "client-a", 3, "2026-08-03T10:00:03+00:00"),
            item("client-a:ocr", "client-a", 3, "2026-08-03T10:00:03+00:00"),
            item("client-b", "client-b", 2, "2026-08-03T10:00:02+00:00"),
        ]
    )

    assert state["s-kswl"]["hostile_count"] == 3
    assert store._hostile_system_state(
        [
            item("client-a:old", "client-a", 2, "2026-08-03T10:00:00+00:00"),
            item("client-a:clear", "client-a", 0, "2026-08-03T10:00:04+00:00"),
        ]
    ) == {}


def test_postgres_hostile_wave_state_excludes_friendly_ocr_from_visual_counts():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._active_intel = {}
    store._scorer = None

    def item(active_id, target_type, metadata, name=""):
        return ActiveIntelItem(
            active_id=active_id,
            source="eve-sentry-detector",
            source_instance="client-a",
            system_name="S-KSWL",
            target_type=target_type,
            name=name,
            metadata=metadata,
            first_seen_at="2026-08-03T10:00:00+00:00",
            last_seen_at="2026-08-03T10:00:00+00:00",
        )

    state = store._hostile_system_state(
        [
            item(
                "presence",
                "system",
                {
                    "presence_only": True,
                    "hostile_icon_count": 2,
                    "hostile_icon_seen_at": "2026-08-03T10:00:00+00:00",
                },
            ),
            item(
                "friendly",
                "character",
                {
                    "hostile_icon_count": 2,
                    "identity_status": "resolved",
                    "contact_standing": 10.0,
                },
                name="Friendly Pilot",
            ),
        ]
    )

    assert state["s-kswl"]["hostile_count"] == 2


def test_postgres_hostile_wave_state_tracks_resolved_hostile_personnel():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._active_intel = {}
    item = ActiveIntelItem(
        active_id="ocr:hostile",
        source="eve-sentry-detector",
        source_instance="client-a",
        system_name="S-KSWL",
        target_type="character",
        name="CEKC HA MOPE",
        character_id=2118213032,
        metadata={
            "identity_status": "resolved",
            "hostile_count": 1,
            "client_id": "client-a",
        },
        first_seen_at="2026-09-03T17:26:37+00:00",
        last_seen_at="2026-09-03T17:26:37+00:00",
    )

    state = store._hostile_system_state([item])

    assert state["s-kswl"]["personnel"] == [{
        "name": "CEKC HA MOPE",
        "character_id": 2118213032,
        "identity_status": "resolved",
        "first_seen_at": "2026-09-03T17:26:37+00:00",
    }]


def test_postgres_hostile_wave_row_decodes_personnel_with_legacy_rows():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    wave = store._hostile_wave_from_row({
        "wave_id": "wave-1",
        "system_name": "S-KSWL",
        "system_id": 30004759,
        "started_at": "2026-09-03T17:26:27+00:00",
        "last_seen_at": "2026-09-03T17:28:19+00:00",
        "cleared_at": "2026-09-03T17:28:19+00:00",
        "active": 0,
        "peak_hostile_count": 1,
        "personnel_json": '{"character:2118213032":{"name":"CEKC HA MOPE","character_id":2118213032}}',
    })

    assert wave["personnel"] == [{
        "name": "CEKC HA MOPE",
        "character_id": 2118213032,
        "identity_status": "resolved",
        "first_seen_at": "",
    }]


def test_postgres_hostile_wave_persistence_tracks_visual_peak():
    calls = []

    class Result:
        rowcount = 1

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    base = {
        "system_key": "s-kswl",
        "system_name": "S-KSWL",
        "system_id": 30004759,
        "started_at": "2026-08-03T10:00:00+00:00",
        "last_seen_at": "2026-08-03T10:00:03+00:00",
    }

    store._persist_hostile_wave_changes(
        FakeConnection(),
        [
            {"action": "touch", **base, "hostile_count": 2},
            {"action": "touch", **base, "hostile_count": 3},
            {
                "action": "clear",
                **base,
                "hostile_count": 3,
                "cleared_at": "2026-08-03T10:00:06+00:00",
            },
        ],
    )

    assert "peak_hostile_count" in calls[0][0]
    assert "GREATEST" in calls[0][0]
    assert calls[0][1][-2] == 2
    assert calls[0][1][-1] == "{}"
    assert calls[1][1][-2] == 3
    assert calls[1][1][-1] == "{}"
    assert "peak_hostile_count = GREATEST" in calls[2][0]
    assert calls[2][1][2] == 3
    assert calls[2][1][3] == "{}"


def test_postgres_hostile_wave_row_exposes_visual_peak():
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    wave = store._hostile_wave_from_row(
        {
            "wave_id": "wave-1",
            "system_name": "S-KSWL",
            "system_id": 30004759,
            "started_at": "2026-08-03T10:00:00+00:00",
            "last_seen_at": "2026-08-03T10:00:03+00:00",
            "cleared_at": "2026-08-03T10:00:06+00:00",
            "active": 0,
            "peak_hostile_count": 3,
        }
    )

    assert wave["peak_hostile_count"] == 3


def test_postgres_hostile_wave_query_filters_overlapping_lifecycles():
    calls = []

    class Result:
        def fetchall(self):
            return [
                {
                    "wave_id": "wave-1",
                    "system_name": "Tama",
                    "system_id": 30045339,
                    "started_at": "2026-08-03T09:00:00+00:00",
                    "last_seen_at": "2026-08-03T09:05:00+00:00",
                    "cleared_at": "2026-08-03T09:06:00+00:00",
                    "active": 0,
                }
            ]

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())

    waves = store.list_hostile_waves(
        since="2026-08-03T00:00:00+00:00",
        limit=25,
    )

    assert waves[0]["id"] == "wave-1"
    assert waves[0]["active"] is False
    assert "COALESCE(NULLIF(cleared_at, '')" in calls[0][0]
    assert "LIMIT %s" in calls[0][0]
    assert calls[0][1] == ("2026-08-03T00:00:00+00:00", 25)


def test_postgres_ocr_snapshot_persists_old_system_as_inactive():
    persisted_rows = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, query, params):
            return SimpleNamespace(rowcount=1)

    old_item = ActiveIntelItem(
        active_id="old-active",
        source="eve-sentry-detector",
        source_instance="EVE - Pilot A",
        system_name="S-KSWL",
        name="Alice",
        metadata={"client_id": "detector-client:test:pilot-a"},
        first_seen_at="2026-07-03T10:00:00+00:00",
        last_seen_at="2026-07-03T10:00:00+00:00",
    )
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._active_intel = {old_item.active_id: old_item}
    store._reports = []
    store._resolver = None
    store._enricher = None
    store._ocr_missing_counts = {}
    store._alert_cache = {}
    store._lock = threading.RLock()
    store._connect = FakeConnection
    store._assign_report_stream_positions = lambda _connection, reports: [
        setattr(report, "stream_position", index)
        for index, report in enumerate(reports, start=1)
    ]
    store._refresh_report_stream_positions = lambda _connection, _reports: None
    store._active_row = lambda item: item.to_dict()
    store._upsert_active_intel_rows = (
        lambda _connection, rows: persisted_rows.extend(rows)
    )
    store._reset_ocr_alert_cooldown = lambda _item: None

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test:pilot-a",
            "source_instance": "EVE - Pilot A",
            "system_name": "HB-FSO",
            "names": [],
            "seen_at": "2026-07-03T10:00:10+00:00",
        }
    )

    assert result["expired"] == 1
    assert old_item.active is False
    assert old_item.left_at == "2026-07-03T10:00:10+00:00"
    assert old_item.metadata["left_reason"] == "system_changed"
    assert persisted_rows == [old_item.to_dict()]


def test_postgres_ocr_snapshot_uses_complete_roster_not_coordinate_evidence(tmp_path):
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def executemany(self, _query, _rows):
            return None

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._load_reports = lambda: []
    IntelStore.__init__(store, tmp_path / "intel.json", systems={}, links=[])
    store._connect = FakeConnection
    store._assign_report_stream_positions = lambda _connection, reports: [
        setattr(report, "stream_position", index)
        for index, report in enumerate(reports, start=1)
    ]
    store._refresh_report_stream_positions = lambda _connection, _reports: None
    store._upsert_active_intel_rows = lambda _connection, _rows: None
    store._persist_hostile_wave_changes = lambda _connection, _changes: None

    try:
        result = store.record_ocr_snapshot(
            {
                "client_id": "detector-client:test",
                "source_instance": "EVE - Pilot",
                "system_name": "S-KSWL",
                "hostile_icon_count": 1,
                "names": ["Friendly Pilot", "Enemy Pilot"],
                "ocr_candidates": [
                    {
                        "text": "Friendly Pilot",
                        "left": 20,
                        "top": 4,
                        "right": 100,
                        "bottom": 14,
                    },
                    {
                        "text": "STARKEY",
                        "left": 20,
                        "top": 20,
                        "right": 80,
                        "bottom": 30,
                    },
                    {
                        "text": "07",
                        "left": 84,
                        "top": 20,
                        "right": 100,
                        "bottom": 30,
                    },
                ],
                "hostile_icons": [
                    {"left": 6, "top": 20, "right": 16, "bottom": 30},
                ],
            }
        )
    finally:
        IntelStore.close(store)

    assert result["created"] == 2
    active = store.list_active_intel(source="eve-sentry-detector")
    assert {item["name"] for item in active} == {"Friendly Pilot", "Enemy Pilot"}


def test_postgres_ocr_snapshot_persists_fresh_cached_identity_without_queueing(
    tmp_path,
):
    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def executemany(self, _query, _rows):
            return None

    class CachedResolver:
        def cached_name(self, name, *, allow_stale=False):
            assert name == "Alice"
            assert allow_stale is True
            return (
                SimpleNamespace(
                    name="Alice",
                    category="character",
                    entity_id=123,
                ),
                "cached",
            )

        def cached_character_profile(self, character_id, *, allow_stale=False):
            assert character_id == 123
            assert allow_stale is True
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_name": "Some Corp",
                "cache_status": "cached",
            }

        def enrich_observation(self, observation):
            raise AssertionError(observation)

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._load_reports = lambda: []
    IntelStore.__init__(
        store,
        tmp_path / "intel.json",
        systems={},
        links=[],
        resolver=CachedResolver(),
    )
    store._connect = FakeConnection
    store._assign_report_stream_positions = lambda _connection, reports: [
        setattr(report, "stream_position", index)
        for index, report in enumerate(reports, start=1)
    ]
    store._refresh_report_stream_positions = lambda _connection, _reports: None
    store._upsert_active_intel_rows = lambda _connection, _rows: None
    store._persist_hostile_wave_changes = lambda _connection, _changes: None
    submitted = []
    store._esi_worker.submit = (
        lambda key, task: submitted.append((key, task)) or True
    )

    result = store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Pilot",
            "system_name": "S-KSWL",
            "names": ["Alice"],
        }
    )

    assert result["created"] == 1
    assert submitted == []
    active = store.list_active_intel(source="eve-sentry-detector")[0]
    assert active["character_id"] == 123
    assert active["metadata"]["identity_status"] == "resolved"
    assert active["metadata"]["corporation_name"] == "Some Corp"


def test_postgres_hostile_presence_persists_active_rows_and_wave_changes(tmp_path):
    persisted_rows = []
    persisted_waves = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._load_reports = lambda: []
    IntelStore.__init__(store, tmp_path / "intel.json", systems={}, links=[])
    store._connect = FakeConnection
    store._upsert_active_intel_rows = (
        lambda _connection, rows: persisted_rows.append(list(rows))
    )
    store._persist_hostile_wave_changes = (
        lambda _connection, changes: persisted_waves.append(list(changes))
    )
    payload = {
        "client_id": "detector-client:test",
        "source_instance": "EVE - Pilot",
        "system_name": "S-KSWL",
        "system_id": 30004759,
    }

    try:
        created = store.record_hostile_presence(
            {
                **payload,
                "hostile_icon_count": 2,
                "seen_at": "2026-08-07T10:00:00+00:00",
            }
        )
        cleared = store.record_hostile_presence(
            {
                **payload,
                "hostile_icon_count": 0,
                "seen_at": "2026-08-07T10:00:10+00:00",
            }
        )
    finally:
        IntelStore.close(store)

    assert created["created"] == 1
    assert cleared["expired"] == 1
    assert len(persisted_rows) == 2
    assert persisted_rows[0][0][5] == "system"
    assert json.loads(persisted_rows[0][0][9])["hostile_icon_count"] == 2
    assert persisted_rows[0][0][15] == 1
    assert persisted_rows[1][0][15] == 0
    assert persisted_waves[0][0]["action"] == "touch"
    assert persisted_waves[0][0]["system_name"] == "S-KSWL"
    assert persisted_waves[1][0]["action"] == "clear"
    assert persisted_waves[1][0]["cleared_at"] == "2026-08-07T10:00:10+00:00"


def test_postgres_retention_deletes_in_database_without_loading_history():
    calls = []

    class Result:
        rowcount = 1

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._active_intel = {}
    store._reports = []
    store._alert_cache = {}
    store._lock = threading.RLock()

    removed = store.prune_reports_older_than(
        30,
        now="2026-07-30T00:00:00+00:00",
    )

    assert removed == 1
    assert calls[0][0].startswith("WITH active_report_refs AS")
    assert "NOT EXISTS" in calls[0][0]
    assert "jsonb_array_elements_text" in calls[0][0]
    assert "active = 1" in calls[0][0]
    assert calls[0][1] == ("2026-06-30T00:00:00+00:00",)


def test_postgres_prunes_only_inactive_intel_older_than_cutoff():
    calls = []

    class Result:
        rowcount = 1

    class FakeConnection:
        def execute(self, query, params):
            calls.append((" ".join(query.split()), params))
            return Result()

    class FakePoolContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._connect = lambda: _PostgresConnection(FakePoolContext())
    store._active_intel = {}
    store._lock = threading.RLock()

    removed = store.prune_inactive_active_intel_older_than(
        30,
        now="2026-07-30T00:00:00+00:00",
    )

    assert removed == 1
    query, params = calls[0]
    assert query.startswith("DELETE FROM active_intel")
    assert "active = 0" in query
    assert "NULLIF(cleared_at, '')" in query
    assert "NULLIF(left_at, '')" in query
    assert "::timestamptz < %s::timestamptz" in query
    assert "RETURNING active_id" not in query
    assert params == ("2026-06-30T00:00:00+00:00",)


@pytest.mark.parametrize("retention_days", [-1, True, 1.5])
def test_postgres_inactive_intel_retention_rejects_invalid_days(retention_days):
    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)

    with pytest.raises(ValueError):
        store.prune_inactive_active_intel_older_than(retention_days)
