"""Restart-free mode switching and late-result/resource lifetime regressions."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.esi.cache import EsiCache
from app.esi.personnel_archive import AffiliationUpdate, IdentityUpdate
from app.esi.personnel_control import PersonnelController, PersonnelResources
from app.esi.personnel_runtime import PersonnelResolver, PersonnelRuntime
from app.esi.personnel_setup import PersonnelEnricher
from app.esi.resolver import EsiResolver
from app.server.auth import AuthError
from app.server.personnel_settings import DEFAULTS
from tests.auth_test_store import AuthTestStore
from tests.test_personnel_archive import archive_factory  # noqa: F401
from tests.test_personnel_runtime import Client
from tests.test_personnel_settings import settings_service  # noqa: F401


def test_all_modes_switch_without_restarting_or_replacing_worker(settings_service):
    manager, store, _ = settings_service
    worker = store._esi_worker
    state = manager.snapshot()
    first = None
    for mode in ("shadow", "on", "shadow", "off", "on", "off"):
        state = manager.update({"revision": state["revision"], "values": {**DEFAULTS, "mode": mode}}, "admin")
        assert state["effective"]["mode"] == mode
        assert not state["restart_required"] and not state["apply_required"]
        assert store._esi_worker is worker
        if first is None:
            first = store._personnel_runtime
        if mode == "on":
            assert store._resolver.personnel_enabled
        else:
            assert store._resolver is manager.controller.original_resolver
    assert first._stop.is_set()


def test_initialization_failure_leaves_saved_and_running_mode_unchanged(settings_service, monkeypatch):
    manager, store, auth = settings_service
    def fail(values):
        raise AuthError("档案初始化失败", 503, "personnel_switch_failed")
    monkeypatch.setattr(manager.controller, "prepare", fail)
    with pytest.raises(AuthError, match="初始化失败"):
        manager.update({"revision": "environment", "values": {**DEFAULTS, "mode": "on"}}, "admin")
    assert manager.snapshot()["values"] == DEFAULTS
    assert store._personnel_runtime is None
    assert auth.repository.list_audit() == []


def test_read_is_side_effect_free_and_same_saved_values_retry_activation(settings_service, monkeypatch):
    manager, store, auth = settings_service
    publish = manager.controller.publish
    def fail(*args):
        raise RuntimeError("interrupted after persistence")
    monkeypatch.setattr(manager.controller, "publish", fail)
    # No new resource is needed for this scheduling-only change.
    values = {**DEFAULTS, "background_max": 1}
    with pytest.raises(AuthError, match="保存结果未确认"):
        manager.update({"revision": "environment", "values": values}, "admin")
    state = manager.snapshot()
    assert state["values"] == values
    assert store._personnel_runtime is None
    monkeypatch.setattr(manager.controller, "publish", publish)
    manager.update({"revision": state["revision"], "values": values}, "admin")
    assert len(auth.repository.list_audit()) == 1


def test_stopped_scheduler_pool_is_closed_only_after_inflight_work_finishes(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    controller = PersonnelController(store, SimpleNamespace(), None)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def drain(*, timeout):
        assert timeout is None
        entered.set()
        assert release.wait(3)
    runtime = SimpleNamespace(mode="on", request_stop=lambda: None, close=drain)
    resource = PersonnelResources(SimpleNamespace(close=closed.set), runtime, None, None)
    controller.resources = resource
    store._personnel_runtime = runtime
    try:
        controller.publish(DEFAULTS, None)
        assert entered.wait(1)
        assert store._personnel_runtime is None
        assert not closed.is_set()
        # A visual clear can still acquire the state lock while old work drains.
        store.record_hostile_presence({"client_id": "node", "system_name": "Tama", "hostile_icon_count": 0})
    finally:
        release.set()
        controller.close()
        store.close()
    assert closed.is_set()


def test_rapid_reenable_is_bounded_while_old_tasks_are_stuck(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    controller = PersonnelController(store, SimpleNamespace(), None)
    controller.retired = [SimpleNamespace(is_alive=lambda: True)] * 2
    try:
        with pytest.raises(AuthError) as error:
            controller.prepare({**DEFAULTS, "mode": "on"})
        assert error.value.code == "personnel_switch_busy"
        assert controller.prepare(DEFAULTS) is None
    finally:
        store.close()


def test_prepared_runtime_does_not_schedule_before_publication(archive_factory):
    runtime = PersonnelRuntime(archive_factory(), Client())
    called = threading.Event()
    runtime.drain_requests = called.set
    runtime.start(lambda *_: True, paused=True)
    try:
        assert not called.wait(0.05)
        runtime.activate()
        assert called.wait(1)
    finally:
        runtime.close(timeout=None)


@pytest.mark.parametrize("clear", [False, True])
def test_ocr_task_keeps_old_dependencies_but_cannot_publish_old_result(archive_factory, tmp_path, clear):
    entered, release = threading.Event(), threading.Event()
    original = EsiResolver(client=Client(), cache=EsiCache(tmp_path / "legacy.json"))
    def slow_enrich(observation):
        entered.set()
        assert release.wait(3)
        observation.character_ids = [1]
        return observation
    original.enrich_observation = slow_enrich
    original.character_profile = lambda cid: {"character_id": cid, "name": "Pilot 1", "corporation_id": 99}
    store = AuthTestStore(tmp_path / "intel.json", resolver=original)
    store._canonicalize_ocr_name = lambda name: name
    archive = archive_factory()
    archive.save_identity(IdentityUpdate(1, "Pilot 1", 100, 100))
    archive.save_affiliation(AffiliationUpdate(1, 10, 100))
    runtime = PersonnelRuntime(archive, Client())
    runtime._remember(list(archive.get_profiles([1]).values()))
    runtime._ready = threading.Event()
    replacement = PersonnelResolver(original, runtime)
    resource = PersonnelResources(SimpleNamespace(close=lambda: None), runtime, replacement,
                                  PersonnelEnricher(replacement, None))
    controller = PersonnelController(store, SimpleNamespace(), original)
    store._personnel_controller = controller
    try:
        store.record_hostile_presence({"client_id": "node", "system_name": "Tama", "hostile_icon_count": 1})
        store.record_ocr_snapshot({"client_id": "node", "system_name": "Tama", "names": ["Pilot 1"]})
        assert entered.wait(1)
        controller.publish({**DEFAULTS, "mode": "on"}, resource)
        if clear:
            store.record_hostile_presence({"client_id": "node", "system_name": "Tama", "hostile_icon_count": 0})
        release.set()
        assert store.wait_for_esi_idle(3)
        items = [item for item in store._active_intel.values() if item.active and item.target_type == "character"]
        if clear:
            assert items == []
        else:
            assert items[0].metadata["character_profiles"][0]["corporation_id"] == 10
        assert store._character_profile_cache == {}
    finally:
        release.set()
        store.close()


def test_slow_profile_read_uses_one_route_without_polluting_new_cache(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def profile(cid):
        entered.set()
        assert release.wait(3)
        return {"character_id": cid, "name": "Old"}
    old = SimpleNamespace(character_profile=profile)
    store = AuthTestStore(tmp_path / "intel.json", resolver=old)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(store.character_profile, 1)
            assert entered.wait(1)
            with store._lock:
                store._personnel_generation += 1
                store._resolver = SimpleNamespace(character_profile=lambda cid: {"character_id": cid, "name": "New"})
            release.set()
            assert result.result(2)["name"] == "Old"
        assert store._character_profile_cache == {}
        assert store.character_profile(1)["name"] == "New"
    finally:
        release.set()
        store.close()


def test_mode_changes_keep_the_same_sse_connection_open(settings_service):
    from urllib.request import Request, urlopen
    from app.server.http_server import IntelHTTPServer

    manager, store, auth = settings_service
    admin = auth.create_user("switch-admin", "admin-password-123", role="admin")
    key = auth.create_api_key(admin["user_id"], "SSE", admin["user_id"], key_type="service_readonly")
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    request = Request(server.url + "/api/v1/events?timeout=10&heartbeat=0.1&bootstrap=0",
                      headers={"Authorization": f"Bearer {key['secret']}"})
    try:
        with urlopen(request, timeout=3) as response:
            assert response.readline().startswith(b": connected")
            state = manager.snapshot()
            for mode in ("shadow", "on", "off"):
                state = manager.update({"revision": state["revision"], "values": {**DEFAULTS, "mode": mode}}, "admin")
            # Read a keepalive from that very same HTTP response, not a reconnect.
            for _ in range(20):
                line = response.readline()
                assert line != b""
                if line.startswith(b": keepalive"):
                    break
            else:
                pytest.fail("no keepalive after hot switching")
    finally:
        server.stop()
