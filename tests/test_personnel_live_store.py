"""End-to-end archive classification against a disposable PostgreSQL schema."""

import time
import json
from types import SimpleNamespace

import pytest

from tests.test_personnel_postgres import postgres_archive, postgres_dsn  # noqa: F401
from tests.test_personnel_runtime import Client
from app.esi.personnel_archive import IdentityUpdate, AffiliationUpdate
from app.esi.personnel_runtime import PersonnelRuntime, PersonnelResolver
from app.esi.personnel_setup import PersonnelEnricher
from app.esi.personnel_setup import configure_personnel
from app.esi.personnel_backfill import PersonnelBackfill
from app.esi.resolver import EsiResolver
from app.esi.cache import EsiCache
from app.server.postgres_store import PostgreSQLIntelStore
from app.intel.classification import ClassificationEngine
from app.intel.scoring import Watchlist


def test_live_membership_correction_never_clears_visual_presence(postgres_archive, postgres_dsn, tmp_path):
    archive, factory = postgres_archive
    now = time.time()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", now, now))
    archive.save_affiliation(AffiliationUpdate(1, 10, now))
    runtime = PersonnelRuntime(archive, Client())
    runtime._remember(list(archive.get_profiles([1]).values()))
    resolver = PersonnelResolver(EsiResolver(cache=EsiCache(tmp_path / "legacy.json")), runtime)
    dsn = postgres_dsn
    scorer = ClassificationEngine(watchlist=Watchlist(hostile_corporation_ids={10}, friendly_corporation_ids={20}))
    store = PostgreSQLIntelStore(dsn, systems={}, links=[], resolver=resolver, scorer=scorer,
                                enricher=PersonnelEnricher(resolver, None))
    store._personnel_runtime = runtime
    try:
        store.record_hostile_presence({"client_id": "test-node", "system_name": "Tama", "hostile_icon_count": 1})
        store.record_ocr_snapshot({"client_id": "test-node", "system_name": "Tama", "names": ["Pilot 1"]})
        assert store.wait_for_esi_idle(5)
        assert len(store._hostile_system_state()["tama"]["personnel"]) == 1
        archive.save_affiliation(AffiliationUpdate(1, 20, now + 1))
        runtime._remember(list(archive.get_profiles([1]).values()))
        store.refresh_personnel({1})
        assert store.wait_for_esi_idle(5)
        state = store._hostile_system_state()["tama"]
        assert state["hostile_count"] == 1
        assert state["personnel"] == []
        # Friendly membership still has a current observation, so it can become hostile again.
        archive.save_affiliation(AffiliationUpdate(1, 10, now + 2))
        runtime._remember(list(archive.get_profiles([1]).values()))
        store.refresh_personnel({1})
        assert store.wait_for_esi_idle(5)
        assert len(store._hostile_system_state()["tama"]["personnel"]) == 1
        store.record_hostile_presence({"client_id": "test-node", "system_name": "Tama", "hostile_icon_count": 0})
        store.refresh_personnel({1}, True)
        assert store.wait_for_esi_idle(5)
        assert store._hostile_system_state() == {}
        with factory() as connection:
            events = connection.execute("SELECT event_type, payload_json FROM intel_events ORDER BY seq").fetchall()
        assert any(row["event_type"] == "alert.updated" and json.loads(row["payload_json"]).get("hostile_personnel") == [] for row in events), events
    finally:
        store.close()


def test_history_backfill_resumes_without_promoting_cold_people(postgres_archive, postgres_dsn):
    archive, factory = postgres_archive
    dsn = postgres_dsn
    store = PostgreSQLIntelStore(dsn, systems={}, links=[])
    try:
        store.add_observation({"system_name": "Tama", "names": ["Confirmed"], "character_ids": [1],
                               "source": "local_ocr", "seen_at": "2020-01-01T00:00:00+00:00",
                               "metadata": {"identity_status": "resolved", "character_profiles": [
                                   {"character_id": 1, "name": "Confirmed"}]}})
        store.add_observation({"system_name": "Tama", "names": ["Guess"], "source": "local_ocr"})
        backfill = PersonnelBackfill(archive)
        assert backfill.step(limit=1) == 1
        assert archive.find_names(["Confirmed"])["confirmed"]["last_seen_at"] < 1600000000
        assert PersonnelBackfill(archive).step(limit=1) == 1
        assert backfill.step(limit=1) == 0
        assert archive.find_names(["Guess"]) == {}
    finally:
        store.close()


def test_realtime_lane_isolated_from_large_cold_backlog(postgres_archive):
    archive, factory = postgres_archive
    with factory() as connection:
        connection.execute("""
            INSERT INTO personnel_refresh_jobs(kind,entity_key,context_key,priority,next_due_at)
            SELECT 'affiliation', id::text, '', 5, 1 FROM generate_series(1,100000) id
        """)
    archive.request_refresh("resolve", "Immediate Pilot", priority=0, due_at=100)
    started = time.perf_counter()
    leases = archive.claim(now=100, kind="resolve", maximum_priority=1)
    elapsed = time.perf_counter() - started
    assert [lease.entity_key for lease in leases] == ["immediate pilot"]
    assert len(archive.claim(now=100, kind="affiliation", minimum_priority=2, limit=100)) == 100
    print(f"\n100k cold jobs: realtime claim {elapsed * 1000:.2f} ms")


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_opt_in_startup_preserves_schema_and_resumes_worker(postgres_archive, postgres_dsn, tmp_path, mode):
    archive, factory = postgres_archive
    with factory() as connection:
        expected_schema = connection.execute("SELECT current_schema() AS name").fetchone()["name"]
    dsn = postgres_dsn
    resolver = EsiResolver(client=Client(), cache=EsiCache(tmp_path / "cache.json"))
    store = PostgreSQLIntelStore(dsn, systems={}, links=[], resolver=resolver)
    try:
        configure_personnel(store, SimpleNamespace(storage="postgres", postgres_dsn=dsn), resolver,
                            environment={"EVE_SENTRY_PERSONNEL_CACHE": mode})
        runtime = store._personnel_runtime
        assert (store._resolver is resolver) == (mode == "shadow")
        with store._personnel_pool.connection() as connection:
            assert connection.execute("SELECT current_schema() AS name").fetchone()["name"] == expected_schema
        runtime.request("Pilot 1", seen_at=time.time())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not runtime.lookup("Pilot 1"):
            time.sleep(0.05)
        assert runtime.lookup("Pilot 1")["character_id"] == 1
        assert archive.get_profiles([1])[1]["name"] == "Pilot 1"
    finally:
        store.close()
