"""Unknown capture retains danger but is not another arrival notification."""

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
        size = overlay.size()
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 2, "freshness": "unknown"}])
        frame, _, count, status = overlay._rows[0]
        assert status.text() == "上次"
        assert count.text() == "敌 2"
        assert "未知" in frame.toolTip()
        assert overlay.size() == size
        overlay.show_summaries([{"system_name": "S-KSWL", "hostile_count": 2}])
        assert status.text() == "来敌"
        assert frame.toolTip() == ""
    finally:
        overlay.close()


def test_unknown_event_does_not_play_or_notify():
    calls = []
    controller = SimpleNamespace(
        _recent_summaries=[], _apply_local_hostile_counts=lambda: None,
        overlay=SimpleNamespace(show_summaries=lambda _: None, set_status=lambda *args: calls.append(args)),
        _notify=lambda *args: calls.append("unexpected notification"),
    )
    AlertTrayController._on_alert(controller, {"system_name": "S-KSWL", "hostile_count": 1, "freshness": "unknown"})
    assert calls == [("采集异常", "warn")]
    assert controller._recent_summaries[0]["freshness"] == "unknown"


def test_bootstrap_preserves_unknown_semantics():
    rows = sync_alert_summaries_from_bootstrap([], {
        "map": {"systems": [{"name": "S-KSWL", "hostile_count": 1, "freshness": "unknown"}]},
    })
    assert rows[0]["freshness"] == "unknown"
    assert summarize_alert({"system_name": "S-KSWL", "hostile_count": 1, "freshness": "unknown"})["freshness"] == "unknown"
