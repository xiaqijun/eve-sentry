"""Live reconnect replaces old state instead of replaying ended incursions."""

from types import SimpleNamespace

import pytest

from app.alert_client import AlertClientState, AlertEventWorker, AlertTrayController
from app.intel_client import IntelApiError


@pytest.mark.parametrize("disconnect", ["eof", "network", "unexpected"])
@pytest.mark.parametrize("current_count", [0, 3])
def test_reconnect_replaces_state_and_delivers_only_post_snapshot_events(
    tmp_path, disconnect, current_count,
):
    requests, alerts, safes, replacements, notifications = [], [], [], [], []
    watermarks = []
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = []
    controller._local_hostile_counts = {}
    controller._sync_map_accounts = lambda: None
    controller._apply_local_hostile_counts = lambda: None
    controller._stop_continuous_alert_sound = lambda: None
    controller._notify = lambda *args: notifications.append(args)
    controller.overlay = SimpleNamespace(
        show_summaries=lambda rows: None, set_status=lambda *args: None,
    )

    def snapshot(seq, count):
        return {
            "id": f"state:{seq}", "event": "bootstrap",
            "data": {"state_source": "system_current_state", "active_intel": [],
                     "alerts": [], "clients": {"heartbeats": []},
                     "map": {"systems": [{"name": "Tama", "hostile_count": count}]
                             if count else []}},
        }

    class Api:
        def __init__(self, *args, **kwargs):
            pass

        def post_heartbeat(self, **kwargs):
            return {}

        def iter_events(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                yield snapshot(10, 9)
                if disconnect == "network":
                    raise IntelApiError("connection reset")
                if disconnect == "unexpected":
                    raise RuntimeError("reader failed")
                return  # A normal stream rollover uses the same live policy.
            if kwargs["last_event_id"]:
                # Old policy would play these obsolete enter/clear transitions.
                yield {"id": "state:11", "event": "alert",
                       "data": {"id": "state:11", "system_name": "Tama", "hostile_count": 1}}
                yield {"id": "state:12", "event": "safe", "data": {"system_name": "Tama"}}
            yield snapshot(20, current_count)
            # A synthetic ID must not downgrade the stored state watermark.
            yield {"id": "presence_gap", "event": "heartbeat", "data": {}}
            watermarks.append(worker.state.last_event_id())
            yield {"id": "state:21", "event": "alert",
                   "data": {"id": "state:21", "system_name": "Tama", "hostile_count": 4}}
            yield {"id": "state:22", "event": "safe", "data": {"system_name": "Tama"}}
            worker.stop()

    def replace(payload):
        controller._on_bootstrap(payload)
        replacements.append([dict(item) for item in controller._recent_summaries])

    worker = AlertEventWorker(
        "http://unused.invalid", AlertClientState(tmp_path / "state.json"), api_factory=Api,
    )
    worker._sleep_with_stop = lambda _: None
    worker.bootstrap_received.connect(replace)
    worker.status_changed.connect(controller._on_status)
    worker.alert_received.connect(alerts.append)
    worker.safe_received.connect(safes.append)
    worker.run()

    assert [request["last_event_id"] for request in requests] == ["", ""]
    assert all(request["include_bootstrap"] for request in requests)
    assert all(not request.get("since") for request in requests)
    assert len(replacements) == 2
    assert replacements[0][0]["hostile_count"] == 9
    if current_count:
        assert replacements[1][0]["hostile_count"] == current_count
        assert controller._active_alert_systems == {"tama"}
    else:
        assert replacements[1] == []
        assert controller._active_alert_systems == set()
    # Keep connection-error feedback, but never fabricate enter/clear notices.
    expected_errors = {"eof": [], "network": [("EVE Sentry Alert", "connection reset")],
                       "unexpected": [("EVE Sentry Alert", "reader failed")]}
    assert notifications == expected_errors[disconnect]
    assert [item["id"] for item in alerts] == ["state:21"]
    assert len(safes) == 1
    assert watermarks == ["state:20"]
    assert worker.state.last_event_id() == "state:22"


def test_failure_before_bootstrap_retries_without_old_cursor(tmp_path):
    requests, snapshots = [], []
    state = AlertClientState(tmp_path / "state.json")
    state.save_last_event_id("state:10")

    class Api:
        def __init__(self, *args, **kwargs):
            pass

        def post_heartbeat(self, **kwargs):
            return {}

        def iter_events(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                raise IntelApiError("snapshot not received")
            yield {"id": "state:30", "event": "bootstrap", "data": {"active_intel": []}}
            worker.stop()

    worker = AlertEventWorker("http://unused.invalid", state, api_factory=Api)
    worker._sleep_with_stop = lambda _: None
    worker.bootstrap_received.connect(snapshots.append)
    worker.run()
    assert [item["last_event_id"] for item in requests] == ["", ""]
    assert len(snapshots) == 1
    assert state.last_event_id() == "state:30"
