from app.core.heartbeat import (
    build_alert_heartbeat_details,
    build_channel_heartbeat_details,
    build_detector_heartbeat_details,
    monitored_system_names,
    resolve_runtime_identity,
    summarize_heartbeat_error,
)


def test_monitored_system_names_reads_online_detector_targets():
    assert monitored_system_names(
        {
            "heartbeats": [
                {
                    "client_type": "detector_client",
                    "online": True,
                    "details": {
                        "monitoring": True,
                        "system": "S-KSWL",
                        "targets": [
                            {"system_name": "S-KSWL", "monitoring": True},
                            {"system_name": "8-4GQM", "monitoring": True},
                        ],
                    },
                },
                {
                    "client_type": "detector_client",
                    "online": False,
                    "details": {"monitoring": True, "system": "OLD"},
                },
            ]
        }
    ) == ["S-KSWL", "8-4GQM"]


def test_monitored_system_names_ignores_stopped_targets_and_stale_parent_system():
    assert monitored_system_names(
        {
            "heartbeats": [
                {
                    "client_type": "detector_client",
                    "online": True,
                    "details": {
                        "monitoring": True,
                        "system": "S-KSWL",
                        "targets": [
                            {"system_name": "S-KSWL", "monitoring": False}
                        ],
                    },
                }
            ]
        }
    ) == []


def test_monitored_system_names_falls_back_for_active_target_without_location():
    assert monitored_system_names(
        {
            "heartbeats": [
                {
                    "client_type": "detector_client",
                    "online": True,
                    "details": {
                        "monitoring": True,
                        "system": "S-KSWL",
                        "targets": [
                            {"system_name": "Unknown", "monitoring": True}
                        ],
                    },
                }
            ]
        }
    ) == ["S-KSWL"]


def test_build_detector_heartbeat_details_includes_runtime_fields():
    details = build_detector_heartbeat_details(
        monitoring=True,
        system_name="Tama",
        system_source="esi",
        popup_alerts=False,
        window_title="EVE - Pilot A",
        last_action="observation:2",
        last_error="temporary issue",
        client_version="test-build",
        host="detector-host",
        last_success_at="2026-07-01T10:00:00+00:00",
    )

    assert details["mode"] == "monitoring"
    assert details["last_action"] == "observation:2"
    assert details["last_error"] == "temporary issue"
    assert details["client_version"] == "test-build"
    assert details["host"] == "detector-host"
    assert details["last_success_at"] == "2026-07-01T10:00:00+00:00"
    assert details["monitoring"] is True
    assert details["system"] == "Tama"
    assert details["system_source"] == "esi"
    assert details["popup"] is False
    assert details["window"] == "EVE - Pilot A"


def test_build_detector_heartbeat_details_uses_defaults_for_missing_text():
    details = build_detector_heartbeat_details(
        monitoring=False,
        system_name="",
        system_source="",
        popup_alerts=True,
        window_title=" ",
    )

    assert details == {
        "mode": "idle",
        "monitoring": False,
        "system": "Unknown",
        "system_source": "default",
        "popup": True,
    }


def test_build_alert_heartbeat_details_prioritizes_mode_action_and_error():
    details = build_alert_heartbeat_details(
        transport="events",
        popup=True,
        details_enabled=False,
        last_action="events:3",
        last_error="stream offline",
        client_version="test-build",
        host="alert-host",
        last_success_at="2026-07-01T11:00:00+00:00",
    )

    assert details["mode"] == "events"
    assert details["last_action"] == "events:3"
    assert details["last_error"] == "stream offline"
    assert details["client_version"] == "test-build"
    assert details["host"] == "alert-host"
    assert details["last_success_at"] == "2026-07-01T11:00:00+00:00"
    assert details["transport"] == "events"
    assert details["popup"] is True
    assert details["details"] is False


def test_build_channel_heartbeat_details_reports_mode_and_error():
    details = build_channel_heartbeat_details(
        server_parse=True,
        last_action="server_parse:2",
        last_error="temporary post failure",
        client_version="test-build",
        host="channel-host",
        last_success_at="2026-07-01T12:00:00+00:00",
    )

    assert details == {
        "mode": "server_parse",
        "last_action": "server_parse:2",
        "last_error": "temporary post failure",
        "client_version": "test-build",
        "host": "channel-host",
        "last_success_at": "2026-07-01T12:00:00+00:00",
        "server_parse": True,
    }


def test_summarize_heartbeat_error_compacts_whitespace_and_truncates():
    text = summarize_heartbeat_error("  first line\nsecond line\tthird line  ", max_length=18)

    assert text == "first line seco..."


def test_resolve_runtime_identity_prefers_environment(monkeypatch):
    monkeypatch.setenv("EVE_SENTRY_CLIENT_VERSION", "env-version")
    monkeypatch.setenv("EVE_SENTRY_CLIENT_HOST", "env-host")

    identity = resolve_runtime_identity()

    assert identity == {
        "client_version": "env-version",
        "host": "env-host",
    }
