"""Historical system projections never become live nodes or fake clear events."""
import json
from types import SimpleNamespace

import pytest

from app.server.system_state import project_active_items, realtime_event_payload


@pytest.mark.parametrize('count', [0, 1, 100])
def test_unknown_projection_covers_raw_history_without_display(count):
    payload = {'system_name': 'Tama', 'hostile_count': count, 'freshness': 'unknown',
               'hostile_personnel': [{'character_id': 1, 'name': 'Old'}]}
    state = {'system_key': 'tama', 'state_version': 1, 'occurred_at': 'old',
             'payload_json': json.dumps(payload)}
    assert project_active_items([{'system_name': 'Tama', 'name': 'Old'},
                                 {'system_name': 'Other', 'source': 'intel_channel'}], [state]) == [
                                     {'system_name': 'Other', 'source': 'intel_channel'}]
    wire = realtime_event_payload({**payload, 'event_type': 'alert.updated', 'id': 'state:1'})
    assert wire['hostile_count'] == 0
    assert wire['hostile_personnel'] == []
    assert wire['freshness'] == 'unknown'
    assert wire['event_type'] == 'alert.updated'
    assert wire['id'] == 'state:1'
    assert payload['hostile_count'] == count
    assert payload['hostile_personnel']  # history untouched


def test_fresh_event_is_unchanged():
    event = {'system_name': 'Tama', 'hostile_count': 2, 'freshness': 'fresh'}
    assert realtime_event_payload(event) is event


def test_bootstrap_filters_unknown_without_dropping_unrelated_missing_ids():
    from app.server.http_server import IntelRequestHandler

    handler = IntelRequestHandler.__new__(IntelRequestHandler)
    handler._store = lambda: SimpleNamespace(heartbeat_snapshot=lambda: {'heartbeats': []})
    handler._hostile_personnel_snapshot = lambda items: []
    live = {'system_name': 'Other', 'source': 'intel_channel'}
    old = {'id': 'old', 'system_name': 'Tama', 'metadata': {'freshness': 'unknown'}}
    payload = handler._event_bootstrap_payload(
        [old, {'system_name': 'Old', 'metadata': {'freshness': 'unknown'}}, live],
        [{'active_intel_id': 'old', 'system_name': 'Tama'}],
    )
    assert payload['active_intel'] == [live]
    assert payload['alerts'] == []
    assert payload['map']['systems'] == []


def test_capture_offline_does_not_create_monitored_system():
    from app.server.client_status import monitored_system_names

    heartbeat = {'client_type': 'detector_client', 'online': True,
                 'details': {'monitoring': True, 'system_name': 'Tama',
                             'targets': [{'system_name': 'Tama', 'capture_online': False}]}}
    assert monitored_system_names({'heartbeats': [heartbeat]}) == []
