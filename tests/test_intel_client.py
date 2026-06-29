from app.alert_client import build_popup_names, format_report
from app.intel_client import IntelApiClient, ReportPoller
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


def test_alert_client_formats_reports_for_console_and_popup():
    report = {
        "system": "Tama",
        "names": ["Alice", "Bob"],
        "seen_at": "2026-06-29T12:00:00+00:00",
    }

    assert format_report(report) == "2026-06-29T12:00:00+00:00 Tama: Alice, Bob"
    assert build_popup_names([report]) == ["Tama - Alice", "Tama - Bob"]
