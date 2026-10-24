"""Authenticated OCR rows must survive identity enrichment and reach the roster."""

from types import SimpleNamespace

import pytest

from app.core.active_intel import ActiveIntelItem
from app.server.http_server import IntelRequestHandler
from app.server.intel_store import IntelStore
from app.server.postgres_store import PostgreSQLIntelStore


class RosterResolver:
    def __init__(self, *, cached=True):
        self.cached = cached
        self.on_enrich = lambda: None

    def cached_name(self, name, **kwargs):
        if not self.cached:
            return None, "miss"
        return SimpleNamespace(name=name, category="character", entity_id=123), "cached"

    def cached_character_profile(self, character_id, **kwargs):
        return self.character_profile(character_id) if self.cached else None

    def character_profile(self, character_id):
        return {"character_id": character_id, "name": "Enemy Pilot",
                "corporation_id": 10, "corporation_name": "Enemy Corp",
                "contact_standing": -5, "cache_status": "cached"}

    def resolve_names(self, names):
        return [SimpleNamespace(name=name, category="character", entity_id=123)
                for name in names]

    def enrich_observation(self, observation):
        self.on_enrich()
        observation.character_ids = [123]
        return observation


def ocr_payload(**extra):
    return {"client_id": "detector:test", "system_name": "S-KSWL",
            "seat_account_id": "account-a", "seat_key_id": "key-a",
            "names": ["Enemy Pilot"], **extra}


@pytest.fixture
def store(tmp_path):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def executemany(self, *args):
            pass

    store = PostgreSQLIntelStore.__new__(PostgreSQLIntelStore)
    store._load_reports = lambda: []
    IntelStore.__init__(store, tmp_path / "intel.json", systems={}, links=[],
                        resolver=RosterResolver())
    store._connect = Connection
    store._assign_report_stream_positions = lambda conn, reports: None
    store._refresh_report_stream_positions = lambda conn, reports: None
    store._upsert_active_intel_rows = lambda conn, rows: None
    store._persist_hostile_wave_changes = lambda conn, changes: None
    store._persist_intel_events = lambda conn, events: None
    store._reserve_db_write = lambda: None
    store._wait_for_db_write = lambda ticket: None
    store._finish_db_write = lambda ticket: None
    store._ocr_esi_persistence_context = lambda: None
    store._reserve_ocr_esi_persistence = lambda: None
    store._persist_ocr_esi_result = lambda *args, **kwargs: None
    store.queued_tasks = []
    store._esi_worker.submit = lambda key, task: store.queued_tasks.append(task) or True
    store._active_intel["presence:test"] = ActiveIntelItem(
        active_id="presence:test", source="eve-sentry-detector",
        source_instance="detector:test", system_name="S-KSWL", target_type="system",
        metadata={"client_id": "detector:test", "presence_only": True,
                  "hostile_icon_count": 1, "_server_seat_account_id": "account-a",
                  "_server_seat_key_id": "key-a"},
    )
    try:
        yield store
    finally:
        IntelStore.close(store)


def character(store):
    return next(item for item in store._active_intel.values()
                if item.target_type == "character")


def roster(store):
    return store._hostile_system_state()["s-kswl"]["personnel"]


def test_postgres_authenticated_cached_ocr_reaches_roster(store):
    assert store.record_ocr_snapshot(ocr_payload())["created"] == 1
    assert [item["character_id"] for item in roster(store)] == [123]
    assert all("_server_seat_account_id" not in report.metadata
               for report in store._reports)


def test_legacy_row_is_repaired_only_when_observed_again(store):
    store.record_ocr_snapshot(ocr_payload())
    item = character(store)
    item.metadata.pop("_server_seat_account_id", None)
    item.metadata.pop("_server_seat_key_id", None)
    store.record_ocr_snapshot(ocr_payload(names=[]))
    assert roster(store) == []
    assert "_server_seat_account_id" not in item.metadata
    assert store.record_ocr_snapshot(ocr_payload())["refreshed"] == 1
    assert [row["character_id"] for row in roster(store)] == [123]


@pytest.mark.parametrize("query", [False, True])
def test_async_identity_preserves_source_and_query_isolation(store, query):
    store._resolver.cached = False
    store.record_ocr_snapshot(ocr_payload(**({"query_id": "query-one"} if query else {})))
    store._process_ocr_esi_task(store.queued_tasks.pop())
    assert character(store).metadata["identity_status"] == "resolved"
    if query:
        assert character(store).metadata["query_only"] is True
        assert roster(store) == []
    else:
        assert [row["character_id"] for row in roster(store)] == [123]
        assert character(store).metadata["_server_seat_key_id"] == "key-a"


def test_async_identity_keeps_latest_observation_context(store):
    store._resolver.cached = False
    store.record_ocr_snapshot(ocr_payload())

    def refresh_during_enrichment():
        store.record_ocr_snapshot(ocr_payload(seat_account_id="account-b", seat_key_id="key-b"))
        character(store).metadata["capture"] = {"fingerprint": "latest"}

    store._resolver.on_enrich = refresh_during_enrichment
    store._process_ocr_esi_task(store.queued_tasks.pop())
    assert character(store).metadata["identity_status"] == "resolved"
    assert character(store).metadata["_server_seat_key_id"] == "key-b"
    assert character(store).metadata["capture"] == {"fingerprint": "latest"}
    assert roster(store) == []


def test_unattributed_refresh_cannot_inherit_previous_seat_identity(store):
    store.record_ocr_snapshot(ocr_payload())
    store.record_ocr_snapshot(ocr_payload(seat_account_id="", seat_key_id=""))
    assert not character(store).metadata.get("_server_seat_account_id")
    assert roster(store) == []


@pytest.mark.parametrize("principal", [None, SimpleNamespace(is_seat=False)])
def test_http_ocr_discards_untrusted_seat_claims(principal):
    handler = IntelRequestHandler.__new__(IntelRequestHandler)
    handler._auth_principal = principal
    payload = ocr_payload()
    result = handler._attributed_ocr_payload(payload)
    assert result["seat_account_id"] == result["seat_key_id"] == ""
    assert payload["seat_account_id"] == "account-a"


def test_http_ocr_uses_authenticated_seat_claims():
    handler = IntelRequestHandler.__new__(IntelRequestHandler)
    handler._auth_principal = SimpleNamespace(is_seat=True, account_id="real", api_key_id="real-key")
    result = handler._attributed_ocr_payload(ocr_payload())
    assert (result["seat_account_id"], result["seat_key_id"]) == ("real", "real-key")
