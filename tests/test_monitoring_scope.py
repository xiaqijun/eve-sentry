"""Monitoring map boundaries, persistence and wire compatibility regressions."""

import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.server.http_server import IntelHTTPServer, IntelRequestHandler, _monitoring_target_state
from app.server.intel_store import IntelStore
from app.server.map_config import MapConfigStore
from app.server.map_settings import update_settings, restore_monitoring_config, settings_path
from app.server.monitoring_scope import MonitoringScope, current_scope, scoped_heartbeat

SYSTEMS = [
    {"name": "A", "system_id": 1, "region_id": 10, "region": "One", "x": 1, "y": 2},
    {"name": "B", "system_id": 2, "region_id": 10, "region": "One", "x": 3, "y": 4},
    {"name": "C", "system_id": 3, "region_id": 20, "region": "Two", "x": 5, "y": 6},
]


@pytest.fixture
def configured(tmp_path):
    config = MapConfigStore(tmp_path / "map.json")
    store = IntelStore(tmp_path / "intel.json")
    with patch("app.server.map_settings.catalogue", return_value=(SYSTEMS, [{"from": "A", "to": "B"}])):
        yield config, store


def selection(**overrides):
    return {"enabled": True, "version": "", "region_ids": [10], "system_ids": [3], "excluded_system_ids": [2], **overrides}


def test_scope_region_union_and_exclusion_persist_independently(configured):
    config, store = configured
    result = update_settings(config, store, selection())
    assert result["system_count"] == 2
    assert set(store._systems) == {"A", "C"}
    assert store._links == []
    assert current_scope(store).allows("a", 1)
    assert not current_scope(store).allows("B", 2)
    # A normal deployment may replace the packaged base map, not this sidecar.
    restored = MapConfigStore(config.path)
    assert restore_monitoring_config(restored)
    assert restored.to_dict()["monitoring_version"] == result["version"]
    assert set(restored.build_map()[0]) == {"A", "C"}


@pytest.mark.parametrize("overrides", [
    {"region_ids": [999]}, {"system_ids": [True]}, {"excluded_system_ids": [999]},
    {"region_ids": [], "system_ids": []}, {"excluded_system_ids": [1, 2, 3]},
    {"version": "stale"}, {"enabled": "true"},
])
def test_invalid_configuration_preserves_state(configured, overrides):
    config, store = configured
    before = config.to_dict()
    with pytest.raises(ValueError):
        update_settings(config, store, selection(**overrides))
    assert config.to_dict() == before
    assert not settings_path(config).exists()
    assert not current_scope(store).enabled


def test_failed_atomic_write_keeps_old_scope(configured):
    config, store = configured
    with patch("app.server.map_settings.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            update_settings(config, store, selection())
    assert not current_scope(store).enabled
    assert not settings_path(config).exists()


def test_outside_presence_and_ocr_are_acknowledged_without_esi_or_retry(configured):
    config, store = configured
    update_settings(config, store, selection())
    payload = {"client_id": "node", "system_name": "B", "system_id": 2, "hostile_icon_count": 1, "names": ["Pilot"]}
    for method in (store.record_hostile_presence, store.record_ocr_snapshot):
        result = method(payload)
        assert result["reason"] == "outside_monitoring_scope"
        assert result["accepted"] is False
    assert not store._active_intel
    assert not store._reports


def test_heartbeat_keeps_transport_but_not_outside_monitoring(configured):
    config, store = configured
    update_settings(config, store, selection())
    heartbeat = {"client_id": "parent", "client_type": "detector_client", "details": {
        "monitoring": True, "targets": [
            {"client_id": "inside", "monitoring": True, "system_name": "A"},
            {"client_id": "outside", "monitoring": True, "system_name": "B", "hostile_icon_count": 9},
        ]}}
    view = scoped_heartbeat(store, heartbeat)
    assert view["details"]["targets"][1]["local_only"]
    assert not view["details"]["targets"][1]["monitoring"]
    assert view["details"]["monitoring"]
    assert heartbeat["details"]["targets"][1]["monitoring"]  # no destructive read
    store.record_heartbeat(heartbeat)
    nodes = _monitoring_target_state(store.heartbeat_snapshot())
    assert len(nodes) == 1


def test_scope_switch_retires_previous_enemy_not_history(configured):
    config, store = configured
    store.record_hostile_presence({"client_id": "node", "system_name": "B", "hostile_icon_count": 1})
    assert any(item.active for item in store._active_intel.values())
    update_settings(config, store, selection())
    store.expire_active_intel()
    assert not any(item.active for item in store._active_intel.values())
    assert any(item.metadata.get("left_reason") == "outside_monitoring_scope" for item in store._active_intel.values())


def test_filtered_sse_advances_cursor_without_sending_outside_intel(configured):
    config, store = configured
    update_settings(config, store, selection())
    handler = IntelRequestHandler.__new__(IntelRequestHandler)
    handler.server = SimpleNamespace(store=store)
    handler.wfile = io.BytesIO()
    handler._write_sse("alert", "state:4", {"system_name": "B", "hostile_count": 1})
    assert handler.wfile.getvalue() == b"id: state:4\n\n"


def test_identity_mismatch_unknown_and_disabled_policy():
    scope = MonitoringScope(True, "v1", (("A", 1),))
    assert not scope.allows("B", 1)
    assert not scope.allows("A", 2)
    assert not scope.allows("Unknown")
    assert scope.allows("Unknown", 1)
    assert MonitoringScope().allows("Anywhere")


def test_leaving_map_retires_previous_in_scope_presence_immediately(configured):
    config, store = configured
    update_settings(config, store, selection())
    store.record_hostile_presence({"client_id": "node", "system_name": "A", "hostile_icon_count": 2})
    result = store.record_hostile_presence({"client_id": "node", "system_name": "B", "hostile_icon_count": 2})
    assert result["scope_expired"] > 0
    assert not any(item.active for item in store._active_intel.values())


def test_remove_and_immediate_readd_never_restores_old_enemy(configured):
    config, store = configured
    store.record_hostile_presence({"client_id": "node", "system_name": "B", "hostile_icon_count": 2})
    first = update_settings(config, store, selection())
    update_settings(config, store, selection(version=first["version"], excluded_system_ids=[]))
    assert not store.list_active_intel(system="B")


def test_settings_http_requires_admin_and_csrf(configured, tmp_path):
    from app.server.auth import AuthService
    from app.server.auth_store import AuthRepository
    from tests.auth_test_store import AuthTestStore
    from tests.test_http_server import AuthTestResolver, authenticated_request

    config, _ = configured
    store = AuthTestStore(tmp_path / "auth-intel.json")
    auth = AuthService(AuthRepository(store._connect), AuthTestResolver())
    auth.create_user("admin", "admin-password-123", role="admin")
    member = auth.create_user("pilot", "pilot-password-123", role="member")
    key = auth.create_api_key(member["user_id"], "test", member["user_id"])
    server = IntelHTTPServer(store, port=0, auth_service=auth, map_config_store=config)
    server.start()
    try:
        url = f"{server.url}/api/v1/admin/map-settings"
        assert authenticated_request(url)[0] == 401
        assert authenticated_request(url, headers={"Authorization": f"Bearer {key['secret']}"})[0] == 403
        status, headers, body = authenticated_request(f"{server.url}/api/v1/auth/login", "POST",
            {"username": "admin", "password": "admin-password-123"})
        assert status == 200
        session = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        assert authenticated_request(url, headers=session)[0] == 200
        assert authenticated_request(url, "PUT", selection(), session)[0] == 403
        session["X-CSRF-Token"] = body["csrf_token"]
        status, _, body = authenticated_request(url, "PUT", selection(), session)
        assert status == 200
        assert body["settings"]["system_count"] == 2
        assert authenticated_request(url, "PUT", selection(), session)[0] == 400
    finally:
        server.stop()


def test_scope_http_contract(configured):
    from tests.test_http_server import authenticated_request

    config, store = configured
    update_settings(config, store, selection())
    server = IntelHTTPServer(store, port=0, map_config_store=config)
    server.start()
    try:
        status, _, body = authenticated_request(f"{server.url}/api/v1/hostile-presence", "POST",
            {"client_id": "node", "system_name": "B", "hostile_icon_count": 2})
        assert status == 200 and body["local_only"]
        status, _, body = authenticated_request(f"{server.url}/api/v1/clients/heartbeats", "POST",
            {"client_id": "node", "client_type": "detector_client", "details": {
                "monitoring": True, "system_name": "B"}})
        assert status == 201
        assert body["monitoring_scope"]["enabled"] is True
        status, _, body = authenticated_request(f"{server.url}/api/v1/bootstrap")
        assert status == 200
        body = body["bootstrap"]
        assert body["monitoring_scope"]["version"] == current_scope(store).version
        assert not _monitoring_target_state(body["clients"])
        assert {s["name"] for s in body["map"]["systems"]} == {"A", "C"}
    finally:
        server.stop()


def test_small_selection_preserves_coordinate_precision(configured):
    config, store = configured
    # Full-universe thumbnail rounding can collapse nearby systems; retain raw SDE coordinates.
    systems = [{**s, "x": 80, "y": 80, "sde_x": 1e15 + s["system_id"] * 1e9,
                "sde_y": 2e15 + s["system_id"] * 1e9} for s in SYSTEMS]
    with patch("app.server.map_settings.catalogue", return_value=(systems, [])):
        update_settings(config, store, selection())
    assert store._systems["A"].x == 80
    assert store._systems["C"].x == 1280
    assert systems[0]["x"] == 80  # shared catalogue stays immutable


def test_settings_uses_local_sde_catalogue_and_restores_topology(tmp_path):
    from app.server.map_settings import settings_payload
    from tests.test_map_config import _write_sde_fixture

    sde = tmp_path / "sde"
    _write_sde_fixture(sde)
    config = MapConfigStore(tmp_path / "map.json")
    config.update({"sde_path": str(sde)})
    options = settings_payload(config)
    assert options["regions"] == [{"id": 10000033, "name": "The Citadel"}]
    store = IntelStore(tmp_path / "intel.json")
    update_settings(config, store, {"enabled": True, "version": "", "region_ids": [10000033],
        "system_ids": [], "excluded_system_ids": []})
    assert set(store._systems) == {"Tama", "Kedama"}
    assert store._links == [("Tama", "Kedama")]
    restored = MapConfigStore(config.path)
    restore_monitoring_config(restored)
    systems, links = restored.build_map()
    assert systems == store._systems and links == store._links
