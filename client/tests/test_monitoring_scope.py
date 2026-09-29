"""Monitoring boundary isolation without capture or network access."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.alert_client import AlertTrayController
from app.monitoring_scope import allows_remote, apply_window_scope
from app.ui.main_window import MainWindow
from app.ui.reliable_uploads import ReliableUploadManager

SCOPE = {"enabled": True, "version": "v1", "systems": [{"name": "A", "system_id": 1}]}


def test_scope_matching_and_old_server_compatibility():
    assert allows_remote(SCOPE, "a")
    assert not allows_remote(SCOPE, "B", 1)
    assert not allows_remote(SCOPE, "Unknown")
    assert allows_remote(None, "Anywhere")
    assert allows_remote({"enabled": False}, "Anywhere")


def test_scope_refresh_leaves_worker_running_and_updates_local_count():
    worker, manager, controller = Mock(), Mock(), Mock()
    context = {"system_name": "B", "_hostile_icon_count": 2, "window_title": "EVE"}
    window = SimpleNamespace(_monitoring_scope=None, _upload_manager=manager, _alert_controller=controller,
        _workers={"w": worker}, _worker_contexts={"w": context}, _log_message=Mock())
    apply_window_scope(window, SCOPE)
    worker.stop.assert_not_called()
    worker.request_presence_refresh.assert_called_once()
    controller.update_local_hostile_count.assert_called_once_with("B", 2)
    manager.set_monitoring_scope.assert_called_once_with(SCOPE)


def test_local_only_presence_is_not_queued():
    window = SimpleNamespace(_intel_client=Mock(), _uploads_enabled=True, _monitoring_scope=SCOPE,
        _upload_manager=Mock(), _refresh_intel_location=Mock())
    MainWindow._publish_hostile_presence(window, 1, {"system_name": "B"}, refresh_location=False)
    window._upload_manager.submit_presence.assert_not_called()


def test_local_alert_and_clear_survive_server_authority_and_bootstrap():
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._server_authority = True
    controller._monitoring_scope = SCOPE
    controller._monitoring_system_provider = lambda: ["B"]
    controller._recent_summaries = []
    controller._local_hostile_counts = {}
    controller.overlay = Mock()
    controller._play_alert_sound_sequence = Mock()
    controller._notify = Mock()
    controller._sync_map_accounts = Mock()
    controller.update_local_hostile_count("B", 2)
    assert controller._local_hostile_counts == {"b": ("B", 2)}
    controller._on_bootstrap({"state_source": "system_current_state", "monitoring_scope": SCOPE,
        "map": {"systems": [{"name": "A", "hostile_count": 3}]}, "alerts": [], "active_intel": []})
    assert len(controller._recent_summaries) == 1
    assert controller._recent_summaries[0]["system_name"] == "B"
    assert controller._recent_summaries[0]["hostile_count"] == 2
    controller._on_alert({"system_name": "A", "hostile_count": 5})
    assert len(controller._recent_summaries) == 1  # no remote warning outside the map
    controller.update_local_hostile_count("B", 0)
    assert controller._recent_summaries[0]["hostile_count"] == 0
    assert not controller._local_hostile_counts


def test_inside_local_counts_cannot_override_server_and_multiwindow_still_receives_remote():
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._server_authority = True
    controller._monitoring_scope = SCOPE
    controller._monitoring_system_provider = lambda: ["A", "B"]
    controller._on_alert = Mock()
    controller.update_local_hostile_count("A", 9)
    controller._on_alert.assert_not_called()
    assert controller._remote_alerts_allowed()


def test_uploader_discards_restored_outside_snapshot_without_request():
    manager = SimpleNamespace(_monitoring_scope=SCOPE, _discard=Mock(), _client=Mock())
    upload = SimpleNamespace(key="presence:node", payload={"system_name": "B"})
    ReliableUploadManager._send(manager, upload)
    manager._discard.assert_called_once_with(upload)
    manager._client.post_hostile_presence.assert_not_called()


def test_bootstrap_delivers_scope_to_embedded_upload_controller():
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = []
    controller._local_hostile_counts = {}
    controller._monitoring_scope_callback = Mock()
    controller.overlay = Mock()
    controller._sync_map_accounts = Mock()
    controller._on_bootstrap({"monitoring_scope": SCOPE, "alerts": [], "active_intel": []})
    controller._monitoring_scope_callback.assert_called_once_with(SCOPE)
