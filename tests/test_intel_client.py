import io
import json

from app.alert_client import (
    ack_emitted_alerts,
    build_popup_names,
    emit_alerts,
    format_alert,
    format_report,
    parse_args,
)
from app.intel_client import AlertPoller, IntelApiClient, ReportPoller
from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


class FakeApi:
    def __init__(self, batches):
        self.batches = list(batches)

    def list_reports(self, limit=50):
        _ = limit
        if not self.batches:
            return []
        return self.batches.pop(0)

    def list_alerts(self, limit=50):
        return self.list_reports(limit=limit)

    def stream_alerts(self, limit=50, timeout=30.0):
        _ = timeout
        return self.list_reports(limit=limit)

    def ack_alert(self, alert_id, acknowledged_by="", note=""):
        _ = alert_id, acknowledged_by, note
        return {}


def test_intel_api_client_posts_and_lists_reports(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        created = api.post_report(
            system="Tama",
            names=["Alice"],
            source="test-detector",
            seen_at="2026-06-29T12:00:00+00:00",
        )
        report_id = created["report"]["id"]

        reports = api.list_reports(system="tama", limit=10)

        assert [report["id"] for report in reports] == [report_id]
        assert reports[0]["names"] == ["Alice"]

        observations = api.list_observations(system="tama", limit=10)
        alerts = api.list_alerts(limit=10)

        assert [item["id"] for item in observations] == [report_id]
        assert [item["source_observation_id"] for item in alerts] == [report_id]
    finally:
        server.stop()


def test_intel_api_client_posts_observations(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        created = api.post_observation(
            system_name="Tama",
            names=["Alice"],
            source="intel_channel",
            raw_text="Tama Alice",
            metadata={"hostile_count": 1, "sender": "Scout A"},
            seen_at="2026-06-29T12:00:00+00:00",
        )

        observation_id = created["observation"]["id"]
        assert created["alert"]["id"] == f"evt_{observation_id}"
        assert created["observation"]["metadata"]["sender"] == "Scout A"
        assert api.list_alerts()[0]["score"] == 30
        assert api.stream_alerts(timeout=0)[0]["id"] == f"evt_{observation_id}"

        acked = api.ack_alert(
            f"evt_{observation_id}",
            acknowledged_by="client",
            note="handled",
        )
        assert acked["acknowledged"] is True
        assert acked["acknowledged_by"] == "client"
    finally:
        server.stop()


def test_report_poller_ignores_seeded_reports_and_returns_newest_batch_in_order():
    existing = [{"id": "old", "seen_at": "1", "names": ["Old"]}]
    latest_first = [
        {"id": "new-2", "seen_at": "3", "names": ["Carol"]},
        {"id": "new-1", "seen_at": "2", "names": ["Bob"]},
        {"id": "old", "seen_at": "1", "names": ["Old"]},
    ]
    api = FakeApi([existing, latest_first])
    poller = ReportPoller(api)

    poller.seed_existing()

    assert [report["id"] for report in poller.poll_new()] == ["new-1", "new-2"]


def test_alert_poller_ignores_seeded_alerts_and_returns_newest_batch_in_order():
    existing = [{"id": "old", "created_at": "1", "names": ["Old"]}]
    latest_first = [
        {"id": "new-2", "created_at": "3", "names": ["Carol"]},
        {"id": "new-1", "created_at": "2", "names": ["Bob"]},
        {"id": "old", "created_at": "1", "names": ["Old"]},
    ]
    api = FakeApi([existing, latest_first])
    poller = AlertPoller(api)

    poller.seed_existing()

    assert [alert["id"] for alert in poller.poll_new()] == ["new-1", "new-2"]


def test_alert_poller_reads_event_stream_in_stream_order():
    stream_order = [
        {"id": "new-1", "created_at": "2", "names": ["Bob"]},
        {"id": "new-2", "created_at": "3", "names": ["Carol"]},
    ]
    api = FakeApi([stream_order])
    poller = AlertPoller(api)

    assert [alert["id"] for alert in poller.stream_new(timeout=0)] == [
        "new-1",
        "new-2",
    ]


def test_alert_client_formats_reports_for_console_and_popup():
    report = {
        "system": "Tama",
        "names": ["Alice", "Bob"],
        "seen_at": "2026-06-29T12:00:00+00:00",
    }

    assert format_report(report) == "2026-06-29T12:00:00+00:00 Tama: Alice, Bob"
    assert build_popup_names([report]) == ["Tama - Alice", "Tama - Bob"]

    alert = {
        "system_name": "Tama",
        "names": ["Alice"],
        "created_at": "2026-06-29T12:00:00+00:00",
        "level": "medium",
        "score": 40,
    }
    assert (
        format_alert(alert)
        == "MEDIUM 2026-06-29T12:00:00+00:00 Tama: Alice (score 40)"
    )

    alert["evidence"] = [
        {"type": "local_ocr_seen", "weight": 40, "summary": "Local OCR saw Alice"},
        {"type": "blacklist_match", "weight": 80, "summary": "Blacklisted pilot"},
    ]
    assert (
        format_alert(alert)
        == "MEDIUM 2026-06-29T12:00:00+00:00 Tama: Alice "
        "(score 40) - Local OCR saw Alice; Blacklisted pilot"
    )


def test_alert_client_parse_args_supports_one_shot_json_mode():
    args = parse_args(
        [
            "--once",
            "--json",
            "--poll",
            "--include-existing",
            "--ack",
            "--ack-by",
            "cli",
            "--ack-note",
            "handled",
        ]
    )

    assert args.once is True
    assert args.json is True
    assert args.poll is True
    assert args.ignore_existing is False
    assert args.ack is True
    assert args.ack_by == "cli"
    assert args.ack_note == "handled"


def test_alert_client_emit_alerts_supports_text_and_json_lines():
    alert = {
        "id": "evt-1",
        "system_name": "Tama",
        "names": ["Alice"],
        "created_at": "2026-06-29T12:00:00+00:00",
        "level": "high",
        "score": 70,
    }
    text_stream = io.StringIO()
    json_stream = io.StringIO()

    emit_alerts([alert], stream=text_stream)
    emit_alerts([alert], json_lines=True, stream=json_stream)

    assert text_stream.getvalue().strip() == (
        "[ALERT] HIGH 2026-06-29T12:00:00+00:00 Tama: Alice (score 70)"
    )
    assert json.loads(json_stream.getvalue()) == alert


def test_alert_client_acknowledges_emitted_alerts():
    class AckApi:
        def __init__(self):
            self.acks = []

        def ack_alert(self, alert_id, acknowledged_by="", note=""):
            self.acks.append((alert_id, acknowledged_by, note))
            return {"id": alert_id, "acknowledged": True}

    api = AckApi()

    count = ack_emitted_alerts(
        api,
        [{"id": "evt-1"}, {"id": ""}, {"names": ["missing"]}],
        acknowledged_by="cli",
        note="handled",
    )

    assert count == 1
    assert api.acks == [("evt-1", "cli", "handled")]
