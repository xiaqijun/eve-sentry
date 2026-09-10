"""A real gateway response repairs unknown affiliation without reviving cleared intel."""

import json
from pathlib import Path
import threading
import time

import pytest

from tests.test_personnel_postgres import postgres_archive, postgres_dsn  # noqa: F401
from app.esi.cache import EsiCache
from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate
from app.esi.personnel_runtime import PersonnelResolver, PersonnelRuntime
from app.esi.personnel_setup import PersonnelEnricher
from app.esi.remote import RemoteEsiClient
from app.esi.resolver import EsiResolver
from app.intel.classification import ClassificationEngine
from app.intel.scoring import Watchlist
from app.server.postgres_store import PostgreSQLIntelStore


@pytest.mark.parametrize("clear_before_refresh", [False, True])
def test_gateway_refresh_restores_current_hostile_only(
    postgres_archive, postgres_dsn, tmp_path, monkeypatch, clear_before_refresh,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "esi-gateway"))
    from esi_gateway.server import GatewayServer, GatewayState

    archive, factory = postgres_archive
    now = time.time()
    archive.save_identity(IdentityUpdate(1, "Pilot", now, now))
    archive.save_affiliation(AffiliationUpdate(1, 10, 0))
    gateway = GatewayState("t" * 32, {"127.0.0.1"}, 60, 1000)
    gateway.client.get_character_affiliations = lambda ids: [{"character_id": 1, "corporation_id": 10}]
    server = GatewayServer(("127.0.0.1", 0), gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = RemoteEsiClient(f"http://127.0.0.1:{server.server_port}", "t" * 32)
    runtime = PersonnelRuntime(archive, client)
    runtime._remember(list(archive.get_profiles([1]).values()))
    resolver = PersonnelResolver(EsiResolver(client=client, cache=EsiCache(tmp_path / "legacy.json")), runtime)
    scorer = ClassificationEngine(watchlist=Watchlist(hostile_corporation_ids={10}))
    store = PostgreSQLIntelStore(postgres_dsn, systems={}, links=[], resolver=resolver,
                                scorer=scorer, enricher=PersonnelEnricher(resolver, None))
    store._personnel_runtime = runtime
    try:
        presence = {"client_id": "test-node", "system_name": "Tama", "hostile_icon_count": 1}
        store.record_hostile_presence(presence)
        store.record_ocr_snapshot({"client_id": "test-node", "system_name": "Tama", "names": ["Pilot"]})
        assert store.wait_for_esi_idle(5)
        assert runtime.profile(1)["affiliation_trusted"] is False
        assert store._hostile_system_state()["tama"]["personnel"] == []
        if clear_before_refresh:
            store.record_hostile_presence({**presence, "hostile_icon_count": 0})
        # Repair via the normal active-priority refresh path, without clearing SQL data.
        runtime.set_active([(1, "Pilot", now)])
        runtime.drain_requests()
        assert runtime.run_batch("affiliation", realtime=True) == 1
        assert runtime.profile(1)["affiliation_trusted"] is True
        assert archive.get_profiles([1])[1]["affiliation_fetched_at"] > 0
        assert 1 in runtime._changed
        store.refresh_personnel({1})
        assert store.wait_for_esi_idle(5)
        states = store._hostile_system_state()
        with factory() as connection:
            events = connection.execute("SELECT event_type, payload_json FROM intel_events ORDER BY seq").fetchall()
        updates = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "alert.updated"]
        if clear_before_refresh:
            assert states == {}
            assert not any(item.get("hostile_personnel") for item in updates)
        else:
            assert states["tama"]["hostile_count"] == 1
            assert [item["character_id"] for item in states["tama"]["personnel"]] == [1]
            assert any(item.get("hostile_personnel", [{}])[0].get("character_id") == 1
                       for item in updates if item.get("hostile_personnel"))
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        gateway.close()
