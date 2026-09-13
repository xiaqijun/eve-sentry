"""Live-only list/map state and quiet repeated capture acknowledgements."""
from types import SimpleNamespace

import pytest

from app.alert_client import AlertTrayController, monitored_accounts_from_bootstrap, sync_alert_summaries_from_bootstrap
from app.core.heartbeat import monitored_system_names
from app.ui.main_window import MainWindow


def test_many_unknown_systems_do_not_create_startup_tiles():
    payload = {'map': {'systems': [{'name': f'Old-{i}', 'hostile_count': i,
                                    'freshness': 'unknown'} for i in range(69)]},
               'active_intel': [], 'alerts': [], 'clients': {'heartbeats': []}}
    assert sync_alert_summaries_from_bootstrap([], payload) == []


def test_offline_capture_is_not_a_green_tile_or_map_account():
    heartbeat = {'client_type': 'detector_client', 'online': True, 'health_status': 'online',
                 'details': {'monitoring': True, 'system': 'Tama', 'targets': [
                     {'system_name': 'Tama', 'capture_online': False}]}}
    payload = {'clients': {'heartbeats': [heartbeat]}, 'map': {'systems': []}}
    assert monitored_system_names(payload['clients']) == []
    assert monitored_accounts_from_bootstrap(payload) == []
    assert sync_alert_summaries_from_bootstrap([], payload) == []


def test_disconnect_removes_remote_state_and_stops_sound_without_safe():
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = [{'system_name': 'Tama', 'hostile_count': 3}]
    controller._remote_map_accounts = [{'system_name': 'Tama'}]
    controller._sync_map_accounts = lambda: None
    stopped = []
    controller._stop_continuous_alert_sound = lambda: stopped.append(True)
    controller.overlay = SimpleNamespace(show_summaries=lambda rows: None, set_status=lambda *args: None)
    controller._notify = lambda *args: (_ for _ in ()).throw(AssertionError('unexpected notification'))
    controller._on_status('reconnecting', '')
    assert controller._recent_summaries == []
    assert controller._remote_map_accounts == []
    assert stopped == [True]


def test_presence_log_changes_only_on_semantic_change_or_recovery():
    window = MainWindow.__new__(MainWindow)
    messages = []
    window._log_message = messages.append
    window._update_window_status = lambda *args, **kwargs: None
    context = {'window_title': 'Test', 'system_name': 'Tama', '_capture': {'session_id': 'a'}}
    metadata = {'context': context, 'hostile_icon_count': 2}
    for _ in range(20):
        window._handle_presence_publish_success(metadata)
    assert len(messages) == 1
    window._handle_presence_publish_success({**metadata, 'hostile_icon_count': 1})
    assert len(messages) == 2
    context['_capture']['session_id'] = 'b'
    window._handle_presence_publish_success(metadata)
    assert len(messages) == 3
    window._handle_presence_publish_error(RuntimeError('offline'), metadata)
    window._handle_presence_publish_success(metadata)
    assert len(messages) == 5


@pytest.mark.parametrize('map_payload', [None, {'systems': []}])
def test_legacy_unknown_roster_cannot_restore_live_enemy(map_payload):
    previous = [{'system_name': 'Tama', 'hostile_count': 3, 'active_intel_id': 'old'}]
    payload = {'map': map_payload,
               'active_intel': [{'id': 'old', 'system_name': 'Tama',
                                 'metadata': {'freshness': 'unknown', 'hostile_count': 3}}],
               'alerts': [{'id': 'alert', 'active_intel_id': 'old', 'system_name': 'Tama'}]}
    assert sync_alert_summaries_from_bootstrap(previous, payload) == []
