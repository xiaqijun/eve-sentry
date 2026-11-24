"""Startup must show current intel without replaying inactive-session alerts."""

from PyQt6.QtWidgets import QApplication

from app.alert_client import AlertClientState, AlertEventWorker, AlertOverlay
from app.intel_client import IntelApiError


def test_startup_skips_disk_history_but_network_reconnect_resumes(tmp_path):
    state = AlertClientState(tmp_path / "alerts.json")
    state.save_last_event_id("state:900")
    requests, alerts, safes, snapshots = [], [], [], []

    class Api:
        def __init__(self, *args, **kwargs):
            pass

        def iter_events(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                if kwargs["last_event_id"]:
                    # A persisted cursor opts into replay of missed history.
                    for seq in range(901, 921):
                        yield {
                            "id": f"state:{seq}",
                            "event": "alert" if seq % 2 else "safe",
                            "data": {"id": f"state:{seq}", "system": "S-KSWL"},
                        }
                yield {
                    "id": "state:40",
                    "event": "bootstrap",
                    "data": {"active_intel": []},
                }
                yield {"id": "presence_one", "event": "heartbeat", "data": {}}
                raise IntelApiError("temporary disconnect")
            if kwargs["last_event_id"]:
                yield {
                    "id": "state:41", "event": "alert",
                    "data": {"id": "state:41", "system": "S-KSWL", "hostile_count": 1},
                }
                yield {"id": "state:42", "event": "safe", "data": {"system": "S-KSWL"}}
            yield {"id": "state:42", "event": "bootstrap", "data": {"active_intel": []}}
            worker._stop_requested = True

        def post_heartbeat(self, **kwargs):
            return {}

    worker = AlertEventWorker("http://test.invalid", state, api_factory=Api)
    worker._sleep_with_stop = lambda _: None
    worker.alert_received.connect(alerts.append)
    worker.safe_received.connect(safes.append)
    worker.bootstrap_received.connect(snapshots.append)
    worker.run()

    assert [request["last_event_id"] for request in requests] == ["", ""]
    assert len(snapshots) == 2
    assert alerts == []
    assert safes == []


def test_repeated_visible_state_does_not_rebuild_overlay(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show()
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 1}])
        app.processEvents()
        calls = []
        setter = overlay.set_map_alerts
        def record(alerts):
            calls.append(alerts)
            setter(alerts)

        monkeypatch.setattr(overlay, "set_map_alerts", record)
        overlay.show_summaries(
            [{"system_name": "S-KSWL", "hostile_count": 1, "created_at": "new timestamp"}]
        )
        assert calls == []
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 0}])
        assert len(calls) == 1
        assert overlay._map_widget._alerts[0]["hostile_count"] == 0
        assert overlay._map_widget._alerts[0]["active"] is False
    finally:
        overlay.close()
