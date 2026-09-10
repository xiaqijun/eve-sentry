"""Persisted admin settings, authorization, and live scheduling regression tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.esi.personnel_runtime import PersonnelRuntime
from app.server.auth import AuthError, AuthService
from app.server.auth_store import AuthRepository
from app.server.http_server import IntelHTTPServer
from app.server.personnel_settings import DEFAULTS, PersonnelSettings
from tests.auth_test_store import AuthTestStore
from tests.test_http_server import AuthTestResolver, authenticated_request
from tests.test_personnel_archive import archive_factory  # noqa: F401
from tests.test_personnel_runtime import Client


@pytest.fixture
def settings_service(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    auth = AuthService(AuthRepository(store._connect), AuthTestResolver())
    manager = PersonnelSettings(store, SimpleNamespace(storage="postgres"), auth.resolver, auth, environment={})
    store._personnel_settings = manager
    try:
        yield manager, store, auth
    finally:
        auth.close()
        store.close()


def test_mode_is_persisted_but_not_falsely_reported_as_running(settings_service):
    manager, store, auth = settings_service
    original = manager.snapshot()
    assert original["values"] == DEFAULTS
    assert original["effective"]["mode"] == "off"
    result = manager.update({"revision": original["revision"], "values": {**DEFAULTS, "mode": "shadow"}}, "admin")
    assert result["values"]["mode"] == "shadow"
    assert result["effective"]["mode"] == "off"
    assert result["restart_required"]
    assert getattr(store, "_personnel_runtime", None) is None
    restarted = PersonnelSettings(store, SimpleNamespace(storage="postgres"), auth.resolver, auth,
                                  environment={"EVE_SENTRY_PERSONNEL_CACHE": "on"})
    assert restarted.startup_values()["mode"] == "shadow"
    assert len(auth.repository.list_audit()) == 1
    assert auth.repository.list_audit()[0]["action"] == "personnel.settings_changed"


@pytest.mark.parametrize("patch", [{"mode": "oops"}, {"mode": []}, {"background_max": True},
    {"background_max": 0}, {"background_max": 5}, {"background_max": 1.5},
    {"history_backfill": "false"}, {"background_refresh": 0}, {"unknown": 3}])
def test_settings_strictly_validate_without_persisting(settings_service, patch):
    manager, _, auth = settings_service
    with pytest.raises(ValueError):
        manager.update({"revision": "environment", "values": {**DEFAULTS, **patch}}, "admin")
    assert auth.repository.list_audit() == []
    assert manager.snapshot()["values"] == DEFAULTS


def test_concurrent_saves_cannot_overwrite_another_admin(settings_service):
    manager, _, auth = settings_service
    def save(mode):
        try:
            return manager.update({"revision": "environment", "values": {**DEFAULTS, "mode": mode}}, "admin")
        except AuthError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["on", "shadow"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "settings_conflict" in results
    assert len(auth.repository.list_audit()) == 1


def test_failed_audit_rolls_back_setting_and_does_not_apply_runtime(settings_service, archive_factory, monkeypatch):
    manager, store, auth = settings_service
    runtime = PersonnelRuntime(archive_factory(), Client())
    runtime.mode = "shadow"
    store._personnel_runtime = runtime
    def fail(*args):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(auth.repository, "_insert_audit", fail)
    with pytest.raises(AuthError, match="保存结果未确认"):
        manager.update({"revision": "environment", "values": {**DEFAULTS, "background_refresh": False}}, "admin")
    assert manager.snapshot()["values"] == DEFAULTS
    assert runtime.scheduling_settings()["background_refresh"]


def test_scheduler_changes_apply_without_replacing_runtime(settings_service, archive_factory):
    manager, store, _ = settings_service
    runtime = PersonnelRuntime(archive_factory(), Client())
    runtime.mode = "shadow"
    store._personnel_runtime = runtime
    result = manager.update({"revision": "environment", "values": {
        **DEFAULTS, "mode": "on", "background_refresh": False, "history_backfill": False,
        "background_max": 1}}, "admin")
    assert store._personnel_runtime is runtime
    assert result["effective"] == {"mode": "shadow", "background_refresh": False,
                                    "history_backfill": False, "background_max": 1}
    assert result["restart_required"]
    runtime.request("Pilot 1", seen_at=0)
    runtime.drain_requests()
    assert runtime.run_batch("resolve", realtime=False) == 0
    assert runtime.run_batch("resolve", realtime=True) == 1
    assert len(runtime.client.calls) == 1


def test_unavailable_storage_cannot_be_enabled(settings_service):
    _, store, auth = settings_service
    manager = PersonnelSettings(store, SimpleNamespace(storage="json"), auth.resolver, auth, environment={})
    assert not manager.snapshot()["available"]
    with pytest.raises(AuthError) as error:
        manager.update({"revision": "environment", "values": {**DEFAULTS, "mode": "on"}}, "admin")
    assert error.value.code == "personnel_settings_unavailable"


@pytest.mark.parametrize("enabled", [True, False])
def test_scheduler_honors_history_pause_and_capacity(archive_factory, enabled):
    runtime = PersonnelRuntime(archive_factory(), Client())
    steps = []
    runtime.backfill = SimpleNamespace(step=lambda kind: steps.append(kind))
    runtime.configure_scheduling(background_refresh=True, history_backfill=enabled, background_max=1)
    # Exactly one scheduler cycle; no sleeps or upstream requests.
    runtime._wake.wait = lambda timeout: runtime._stop.set()
    runtime._run()
    assert steps == (["legacy"] if enabled else [])
    assert runtime.snapshot()["background_slots"] <= 1
    assert runtime.client.calls == []


def test_corrupt_persisted_configuration_is_not_silently_replaced(settings_service):
    manager, _, auth = settings_service
    auth.repository.set_setting("personnel_settings", "invalid json", "now")
    with pytest.raises(AuthError) as error:
        manager.startup_values()
    assert error.value.code == "personnel_settings_storage_error"


def test_same_values_are_idempotent(settings_service):
    manager, _, auth = settings_service
    result = manager.update({"values": dict(DEFAULTS), "revision": "environment"}, "admin")
    assert result["revision"] == "environment"
    assert auth.repository.list_audit() == []


def test_database_compare_and_swap_protects_independent_controllers(settings_service):
    first, store, auth = settings_service
    second = PersonnelSettings(store, SimpleNamespace(storage="postgres"), auth.resolver, auth, environment={})
    barrier = Barrier(2)
    for manager in (first, second):
        original_read = manager._read
        def racing_read(read=original_read):
            result = read()
            barrier.wait(timeout=3)
            return result
        manager._read = racing_read
    def save(manager):
        try:
            return manager.update({"revision": "environment", "values": {**DEFAULTS, "mode": "shadow"}}, "admin")
        except AuthError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, (first, second)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "settings_conflict" in results
    assert len(auth.repository.list_audit()) == 1


def test_admin_settings_http_permissions_csrf_validation_and_revision(settings_service):
    manager, store, auth = settings_service
    admin = auth.create_user("settings-admin", "admin-password-123", role="admin")
    member = auth.create_user("settings-member", "member-password-123", role="member")
    member_key = auth.create_api_key(member["user_id"], "Member", admin["user_id"])
    login = auth.login("settings-admin", "admin-password-123", "127.0.0.1")
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    url = server.url + "/api/v1/admin/personnel-settings"
    payload = {"revision": "environment", "values": {**DEFAULTS, "mode": "shadow"}}
    try:
        assert authenticated_request(url)[0] == 401
        assert authenticated_request(url, headers={"Authorization": f"Bearer {member_key['secret']}"})[0] == 403
        assert authenticated_request(url, method="POST", payload=payload,
            headers={"Authorization": f"Bearer {member_key['secret']}"})[0] == 403
        headers = {"Cookie": f"eve_sentry_session={login['session_token']}"}
        status, _, data = authenticated_request(url, headers=headers)
        assert status == 200
        assert data["settings"]["effective"]["mode"] == "off"
        assert authenticated_request(url, method="POST", payload=payload, headers=headers)[0] == 403
        headers["X-CSRF-Token"] = login["csrf_token"]
        assert authenticated_request(url, method="POST", payload={**payload, "extra": True}, headers=headers)[0] == 400
        status, _, data = authenticated_request(url, method="POST", payload=payload, headers=headers)
        assert status == 200
        assert data["settings"]["restart_required"]
        assert authenticated_request(url, method="POST", payload=payload, headers=headers)[0] == 409
        assert manager.snapshot()["values"]["mode"] == "shadow"
    finally:
        server.stop()
