from pathlib import Path

import main as client_main


def test_update_health_marker_contains_exact_client_version(tmp_path, monkeypatch):
    marker = tmp_path / "startup marker.txt"
    monkeypatch.setenv("EVE_SENTRY_CLIENT_VERSION", "1.0.9")

    client_main.write_update_health_marker(str(marker))

    assert marker.read_text(encoding="ascii") == "1.0.9"


def test_update_health_marker_is_delayed_for_eight_seconds(tmp_path, monkeypatch):
    marker = tmp_path / "startup marker.txt"
    scheduled = []
    monkeypatch.setenv("EVE_SENTRY_CLIENT_VERSION", "1.0.9")
    monkeypatch.setattr(
        client_main.QTimer,
        "singleShot",
        lambda delay, callback: scheduled.append((delay, callback)),
    )

    client_main.schedule_update_health_marker(str(marker))

    assert len(scheduled) == 1
    assert scheduled[0][0] == 8000
    assert not marker.exists()
    scheduled[0][1]()
    assert marker.read_text(encoding="ascii") == "1.0.9"
