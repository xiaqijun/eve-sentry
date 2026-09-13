"""Unavailable capture is removed from live UI without announcing safety."""

from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication

from app.alert_client import AlertOverlay, AlertTrayController, summarize_alert, sync_alert_summaries_from_bootstrap


def test_unknown_updates_overlay_without_resize_or_losing_count(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 2}])
        app.processEvents()
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 2, "freshness": "unknown"}])
        frame, _, count, status = overlay._rows[0]
        assert frame.isHidden()
        assert count.text() == ""
        assert status.text() == ""
        assert overlay._map_widget._alerts == []
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 2}])
        assert status.text() == "来敌"
        assert frame.toolTip() == ""
    finally:
        overlay.close()


def test_unknown_event_does_not_play_or_notify():
    calls = []
    controller = AlertTrayController.__new__(AlertTrayController)
    controller._recent_summaries = [{"system_name": "S-KSWL", "hostile_count": 1}]
    controller._remote_map_accounts = [{"system_name": "S-KSWL"}]
    controller._sync_map_accounts = lambda: None
    controller._stop_continuous_alert_sound = lambda: calls.append("sound stopped")
    controller.overlay = SimpleNamespace(show_summaries=lambda _: None, set_status=lambda *args: calls.append(args))
    controller._notify = lambda *args: calls.append("unexpected notification")
    AlertTrayController._on_alert(controller, {"system_name": "S-KSWL", "hostile_count": 1, "freshness": "unknown"})
    assert calls == ["sound stopped", ("采集异常", "warn")]
    assert controller._recent_summaries == []
    assert controller._remote_map_accounts == []
    assert controller._active_alert_systems == set()


def test_bootstrap_preserves_unknown_semantics():
    rows = sync_alert_summaries_from_bootstrap([], {
        "map": {"systems": [{"name": "S-KSWL", "hostile_count": 1, "freshness": "unknown"}]},
    })
    assert rows == []
    assert summarize_alert({"system_name": "S-KSWL", "hostile_count": 1, "freshness": "unknown"})["freshness"] == "unknown"
