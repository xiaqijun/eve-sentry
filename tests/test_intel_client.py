import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.alert_client import (
    AlertClientState,
    AlertEventConsumer,
    AlertOverlay,
    AlertTrayController,
    OVERLAY_HOSTILE_COUNT_WIDTH,
    OVERLAY_GRID_SPACING,
    OVERLAY_MIN_WIDTH,
    RESIZE_BOTTOM,
    RESIZE_RIGHT,
    active_alert_keys_from_bootstrap,
    aggregate_alert_summaries,
    alert_hostile_count,
    build_heartbeat_details,
    format_alert_time,
    parse_args,
    overlay_tile_dimensions,
    prune_inactive_alert_summaries,
    summarize_alert,
    sync_alert_summaries_from_bootstrap,
    update_alert_summaries_active,
)
from app.intel_client import AlertPoller, IntelApiClient, IntelApiError, ReportPoller
from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


def eve_chat_timestamp(offset_seconds: int = 0) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=offset_seconds)
    ).strftime("%Y.%m.%d %H:%M:%S")


class FakeApi:
    def __init__(self, batches):
        self.batches = list(batches)
        self.alert_filters = []
        self.stream_since = []
        self.stream_last_event_ids = []

    def list_reports(self, limit=50):
        _ = limit
        if not self.batches:
            return []
        return self.batches.pop(0)

    def list_alerts(
        self,
        limit=50,
        min_score=None,
        min_level="",
    ):
        self.alert_filters.append((min_score, min_level))
        return self.list_reports(limit=limit)

    def stream_alerts(
        self,
        since="",
        last_event_id="",
        limit=50,
        timeout=30.0,
        min_score=None,
        min_level="",
    ):
        _ = timeout
        self.stream_since.append(since)
        self.stream_last_event_ids.append(last_event_id)
        self.alert_filters.append((min_score, min_level))
        return self.list_reports(limit=limit)

    def iter_alert_events(self, **kwargs):
        for alert in self.stream_alerts(**kwargs):
            yield alert

class RecordingClient(IntelApiClient):
    def __init__(self):
        super().__init__("http://example.invalid")
        self.calls = []

    def _request(self, method, path, payload=None, params=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "params": params,
            }
        )
        if path.endswith("/clients/heartbeats"):
            return {"heartbeat": {"client_id": "client-1", "online": True}}
        if path.endswith("/heartbeats"):
            return {"heartbeats": [], "summary": {"count": 0}, "count": 0}
        if path.endswith("/esi/status"):
            return {"enabled": False, "authenticated": False}
        if path.endswith("/esi/session"):
            return {"snapshot": {"location": None}}
        if "/alerts/" in path:
            return {"detail": {"alert": {"id": "evt-1"}}}
        if path.endswith("/reports"):
            return {"reports": []}
        if path.endswith("/observations"):
            return {"observations": []}
        if path.endswith("/alerts"):
            return {"alerts": []}
        if path.endswith("/clients"):
            return {"clients": {"heartbeats": [], "summary": {"count": 0}}}
        if path.endswith("/config"):
            return {"config": {"schema_version": "scoring_config.v1"}}
        if path.endswith("/bootstrap"):
            return {"bootstrap": {"schema_version": "intel_bootstrap.v1"}}
        if path.endswith("/map/neighborhood"):
            return {"map": {"systems": [], "links": [], "centers": []}}
        if path.endswith("/map"):
            return {"map": {"systems": [], "links": [], "summary": {}}}
        if "/map/systems/" in path:
            return {
                "system": {
                    "profile": {"system_id": 30002813, "name": "Tama"},
                    "intel": {"entity": {"entity_type": "system"}},
                }
            }
        if "/characters/by-name/" in path or path.endswith("/characters/123"):
            return {"character": {"character_id": 123, "name": "Alice Prime"}}
        if path.endswith("/systems/by-name/S-KSWL"):
            return {"system": {"system_id": 30002814, "name": "S-KSWL"}}
        if path.endswith("/systems/30002813"):
            return {"system": {"system_id": 30002813, "name": "Tama"}}
        return {"ok": True, "report": {"id": "r-1"}, "observation": {"id": "o-1"}}


def test_intel_api_client_targets_v1_routes_for_http_requests():
    api = RecordingClient()

    api.post_report(system="Tama", names=["Alice"])
    api.post_observation(system_name="Tama", names=["Alice"])
    api.post_channel_line("Tama Alice", channel="Alliance Intel")
    api.post_heartbeat("client-1", "channel_client")
    api.list_heartbeats()
    api.esi_status()
    api.esi_session(include_location=True, include_contacts=False)
    api.system_profile(30002813)
    api.character_profile(123)
    api.character_by_name("Alice Prime")
    api.system_by_name("S-KSWL")
    api.list_reports()
    api.list_observations()
    api.list_alerts()
    api.alert_detail("evt-1")
    api.bootstrap()
    api.map_snapshot()
    api.map_neighborhood(["Tama"], [30002813], hops=2)
    api.map_system(30002813)

    assert [call["path"] for call in api.calls] == [
        "/api/v1/reports",
        "/api/v1/observations",
        "/api/v1/channel-lines",
        "/api/v1/clients/heartbeats",
        "/api/v1/clients",
        "/api/v1/esi/status",
        "/api/v1/esi/session",
        "/api/v1/systems/30002813",
        "/api/v1/characters/123",
        "/api/v1/characters/by-name/Alice%20Prime",
        "/api/v1/systems/by-name/S-KSWL",
        "/api/v1/reports",
        "/api/v1/observations",
        "/api/v1/alerts",
        "/api/v1/alerts/evt-1",
        "/api/v1/bootstrap",
        "/api/v1/map",
        "/api/v1/map/neighborhood",
        "/api/v1/map/systems/30002813",
    ]
    assert api.calls[-2]["params"] == {
        "systems": "Tama",
        "system_ids": "30002813",
        "hops": "2",
    }


def test_intel_api_client_can_defer_channel_line_enrichment():
    api = RecordingClient()

    api.post_channel_line(
        "Tama Alice",
        channel="Alliance Intel",
        defer_enrichment=True,
    )

    assert api.calls == [
        {
            "method": "POST",
            "path": "/api/v1/channel-lines",
            "payload": {
                "line": "Tama Alice",
                "channel": "Alliance Intel",
                "defer_enrichment": True,
            },
            "params": None,
        }
    ]


def test_intel_api_client_targets_v1_event_stream(monkeypatch):
    captured = {}

    class FakeResponse:
        def __init__(self):
            self.lines = iter(
                [
                    b"id: evt-1\n",
                    b"event: alert\n",
                    b'data: {"id": "evt-1"}\n',
                    b"\n",
                    b"",
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'id: evt-1\nevent: alert\ndata: {"id": "evt-1"}\n\n'

        def readline(self):
            return next(self.lines)

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("app.intel_client.urlopen", fake_urlopen)
    api = IntelApiClient("http://example.invalid", timeout=3.0)

    alerts = api.stream_alerts(last_event_id="evt-0", timeout=0)

    assert alerts == [{"id": "evt-1"}]
    assert captured["url"] == "http://example.invalid/api/v1/events?limit=50&timeout=0"
    assert captured["headers"]["Last-event-id"] == "evt-0"


def test_intel_api_client_iterates_sse_alerts_incrementally(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.lines = iter(
                [
                    b"id: evt-1\n",
                    b"event: alert\n",
                    b'data: {"id": "evt-1", "names": ["Alice"]}\n',
                    b"\n",
                    b"",
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            raise AssertionError("incremental SSE must not call read()")

        def readline(self):
            return next(self.lines)

    monkeypatch.setattr(
        "app.intel_client.urlopen",
        lambda request, timeout=0: FakeResponse(),
    )
    api = IntelApiClient("http://example.invalid", timeout=3.0)

    first = next(api.iter_alert_events(timeout=0))

    assert first == {"id": "evt-1", "names": ["Alice"]}


def test_intel_api_client_iterates_bootstrap_and_alert_events(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.lines = iter(
                [
                    b"id: boot-1\n",
                    b"event: bootstrap\n",
                    b'data: {"alerts": []}\n',
                    b"\n",
                    b"id: evt-1\n",
                    b"event: alert\n",
                    b'data: {"id": "evt-1"}\n',
                    b"\n",
                    b"",
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self.lines)

    monkeypatch.setattr(
        "app.intel_client.urlopen",
        lambda request, timeout=0: FakeResponse(),
    )
    api = IntelApiClient("http://example.invalid", timeout=3.0)

    events = list(api.iter_events(timeout=0))

    assert events == [
        {"id": "boot-1", "event": "bootstrap", "data": {"alerts": []}},
        {"id": "evt-1", "event": "alert", "data": {"id": "evt-1"}},
    ]


def test_intel_api_client_stops_sse_after_keepalive(monkeypatch):
    captured = {}

    class FakeResponse:
        def __init__(self):
            self.readline_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            self.readline_calls += 1
            return b": keepalive\n"

    response = FakeResponse()

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        return response

    stop_checks = 0

    def should_stop():
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 4

    monkeypatch.setattr("app.intel_client.urlopen", fake_urlopen)
    api = IntelApiClient("http://example.invalid", timeout=3.0)

    events = list(
        api.iter_events(
            timeout=30,
            heartbeat=1.0,
            should_stop=should_stop,
            include_bootstrap=True,
        )
    )

    assert events == []
    assert "heartbeat=1.0" in captured["url"]
    assert "bootstrap=true" in captured["url"]
    assert response.readline_calls == 1


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
        assert alerts == []
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
        assert created["alert"]["score"] == 30
        assert created["observation"]["metadata"]["sender"] == "Scout A"

    finally:
        server.stop()


def test_intel_api_client_posts_ocr_snapshot(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url, timeout=1)

        result = api.post_ocr_snapshot(
            client_id="detector-client:test",
            source_instance="EVE - Hajimi6",
            system_name="S-KSWL",
            names=["Alice"],
            seen_at="2026-07-03T10:00:00+00:00",
        )

        assert result["created"] == 1
        assert api.get_active_intel()["count"] == 1
    finally:
        server.stop()


def test_intel_api_client_retries_transient_ocr_snapshot_failure():
    class FlakyClient(IntelApiClient):
        def __init__(self):
            super().__init__("http://example.invalid")
            self.attempts = 0

        def _request(self, method, path, payload=None, params=None):
            _ = method, path, payload, params
            self.attempts += 1
            if self.attempts == 1:
                try:
                    raise TimeoutError("timed out")
                except TimeoutError as exc:
                    raise IntelApiError("timed out") from exc
            return {"refreshed": 1}

    api = FlakyClient()

    result = api.post_ocr_snapshot(
        client_id="detector-client:test",
        source_instance="EVE - Hajimi6",
        system_name="S-KSWL",
        names=["Alice"],
    )

    assert result == {"refreshed": 1}
    assert api.attempts == 2


def test_intel_api_client_posts_hostile_icon_count():
    class RecordingClient(IntelApiClient):
        def __init__(self):
            super().__init__("http://example.invalid")
            self.payload = None

        def _request(self, method, path, payload=None, params=None):
            _ = method, path, params
            self.payload = payload
            return {"created": 1}

    api = RecordingClient()

    api.post_ocr_snapshot(
        client_id="detector-client:test",
        source_instance="EVE - Hajimi6",
        system_name="S-KSWL",
        names=["Alice"],
        hostile_icon_count=1,
    )

    assert api.payload["hostile_icon_count"] == 1


def test_intel_api_client_does_not_retry_non_transport_ocr_snapshot_failure():
    class RejectingClient(IntelApiClient):
        def __init__(self):
            super().__init__("http://example.invalid")
            self.attempts = 0

        def _request(self, method, path, payload=None, params=None):
            _ = method, path, payload, params
            self.attempts += 1
            raise IntelApiError("client_id is required")

    api = RejectingClient()

    with pytest.raises(IntelApiError, match="client_id is required"):
        api.post_ocr_snapshot(
            client_id="",
            source_instance="EVE - Hajimi6",
            system_name="S-KSWL",
            names=[],
        )

    assert api.attempts == 1


def test_intel_api_client_posts_and_lists_heartbeats(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        heartbeat = api.post_heartbeat(
            client_id="channel-client:test",
            client_type="channel_client",
            label="Channel Client",
            heartbeat_interval_seconds=5,
            details={"server_parse": True},
        )
        heartbeats = api.list_heartbeats()

        assert heartbeat["client_id"] == "channel-client:test"
        assert heartbeat["details"]["server_parse"] is True
        assert heartbeats[0]["client_type"] == "channel_client"
    finally:
        server.stop()


def test_intel_api_client_filters_alerts_by_score_and_level(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        low = api.post_channel_line(
            f"[ {eve_chat_timestamp()} ] Scout A > Tama +1 reds",
            channel="Alliance Intel",
        )
        medium = api.post_channel_line(
            f"[ {eve_chat_timestamp(1)} ] Scout B > Tama +3 reds",
            channel="Alliance Intel",
        )
        assert {item["id"] for item in api.list_alerts(min_score=20)} == {
            low["alert"]["id"],
            medium["alert"]["id"],
        }
        assert {item["id"] for item in api.stream_alerts(timeout=0, min_level="low")} == {
            low["alert"]["id"],
            medium["alert"]["id"],
        }
    finally:
        server.stop()


def test_intel_api_client_ignores_sse_keepalive_comments():
    api = IntelApiClient("http://example.invalid")
    body = (
        ": keepalive\n\n"
        "id: evt-1\n"
        "event: alert\n"
        'data: {"id": "evt-1", "names": ["Alice"]}\n\n'
        ": keepalive\n\n"
    )

    assert api._parse_alert_events(body) == [
        {"id": "evt-1", "names": ["Alice"]},
    ]


def test_intel_api_client_posts_raw_channel_lines(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        created = api.post_channel_line(
            f"[ {eve_chat_timestamp()} ] Scout A > Tama +3 reds",
            channel="Alliance Intel",
        )

        assert created["observation"]["system_name"] == "Tama"
        assert created["observation"]["source"] == "intel_channel"
        assert created["observation"]["metadata"]["hostile_count"] == 3
        assert created["alert"]["score"] == 30
        assert api.list_observations(source="intel_channel")[0]["id"] == (
            created["observation"]["id"]
        )
    finally:
        server.stop()


def test_intel_api_client_fetches_esi_session_and_current_system(tmp_path):
    class FakeResolver:
        def resolve_names(self, names):
            assert names == ["Alice"] or names == ["Tama"]
            if names == ["Alice"]:
                return [
                    SimpleNamespace(
                        name="Alice",
                        category="character",
                        entity_id=123,
                    )
                ]
            return [
                SimpleNamespace(
                    name="Tama",
                    category="solar_system",
                    entity_id=30002813,
                )
            ]

        def character_profile(self, character_id):
            assert character_id == 123
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_id": 456,
            }

        def system_profile(self, system_id):
            assert system_id == 30002813
            return {
                "system_id": 30002813,
                "name": "Tama",
                "security_status": 0.3,
            }

    class FakeTokens:
        character_id = 123
        character_owner_hash = "owner-hash"
        scopes = ["esi-location.read_location.v1"]
        expires_at = 2000

        def is_expired(self):
            return False

    class FakeSession:
        def load_tokens(self, refresh_if_needed=True):
            return FakeTokens()

        def snapshot(self, include_location=True, include_contacts=True):
            return SimpleNamespace(
                to_dict=lambda: {
                    "character_id": 123,
                    "character_owner_hash": "owner-hash",
                    "scopes": ["esi-location.read_location.v1"],
                    "location": {"solar_system_id": 30002813},
                    "contacts": [],
                }
            )

    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json", resolver=FakeResolver()),
        port=0,
        esi_session=FakeSession(),
    )
    server.start()
    try:
        api = IntelApiClient(server.url)

        status = api.esi_status()
        assert status["authenticated"] is True
        assert "access_token" not in status

        snapshot = api.esi_session(include_location=True, include_contacts=False)
        assert snapshot["location"]["solar_system_name"] == "Tama"

        system = api.system_profile(30002813)
        assert system["name"] == "Tama"

        character = api.character_profile(123)
        assert character["name"] == "Alice"

        character = api.character_by_name("Alice")
        assert character["character_id"] == 123

        system = api.system_by_name("Tama")
        assert system["system_id"] == 30002813

        current = api.current_esi_system()
        assert current is not None
        assert current["system_id"] == 30002813
        assert current["system_name"] == "Tama"
    finally:
        server.stop()


def test_intel_api_client_fetches_alert_details(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        created = api.post_observation(
            system_name="Tama",
            names=["Alice"],
            source="intel_channel",
            raw_text="Tama Alice",
            seen_at="2026-06-29T12:00:00+00:00",
        )
        detail = api.alert_detail(created["alert"]["id"])

        assert detail["schema_version"] == "alert_detail.v1"
        assert detail["alert"]["id"] == created["alert"]["id"]
        assert detail["observation"]["id"] == created["observation"]["id"]
        assert detail["entities"]["systems"] == [
            {"system_id": None, "name": "Tama"}
        ]
        assert detail["context"]["channel_mentions"] == []
        assert isinstance(detail["explanation"]["degraded_sources"], list)
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


def test_alert_poller_accepts_initial_seen_ids():
    latest_first = [
        {"id": "new", "created_at": "2", "names": ["Bob"]},
        {"id": "old", "created_at": "1", "names": ["Old"]},
    ]
    api = FakeApi([latest_first])
    poller = AlertPoller(api, seen_ids=["old"])

    assert [alert["id"] for alert in poller.poll_new()] == ["new"]


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


def test_alert_poller_sends_since_cursor_to_event_stream():
    api = FakeApi(
        [
            [
                {"id": "new-1", "created_at": "2", "names": ["Bob"]},
                {"id": "new-2", "created_at": "3", "names": ["Carol"]},
            ],
            [
                {"id": "new-3", "created_at": "4", "names": ["Dora"]},
            ],
        ]
    )
    poller = AlertPoller(api)

    assert [alert["id"] for alert in poller.stream_new(timeout=0)] == [
        "new-1",
        "new-2",
    ]
    assert [alert["id"] for alert in poller.stream_new(timeout=0)] == ["new-3"]
    assert api.stream_since == ["", ""]
    assert api.stream_last_event_ids == ["", "new-2"]


def test_alert_poller_seed_and_polling_advance_stream_cursor():
    api = FakeApi(
        [
            [
                {"id": "existing-2", "created_at": "2", "names": ["Old 2"]},
                {"id": "existing-1", "created_at": "1", "names": ["Old 1"]},
            ],
            [
                {"id": "new-3", "created_at": "3", "names": ["New"]},
            ],
            [
                {"id": "new-4", "created_at": "4", "names": ["Stream"]},
            ],
        ]
    )
    poller = AlertPoller(api)

    poller.seed_existing()
    assert [alert["id"] for alert in poller.poll_new()] == ["new-3"]
    assert [alert["id"] for alert in poller.stream_new(timeout=0)] == ["new-4"]
    assert api.stream_since == [""]
    assert api.stream_last_event_ids == ["new-3"]


def test_alert_poller_resumes_real_event_stream_with_last_event_id(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)
        poller = AlertPoller(api)

        first = api.post_channel_line(
            f"[ {eve_chat_timestamp()} ] Scout A > Tama +1 reds",
            channel="Alliance Intel",
        )
        first_alert_id = first["alert"]["id"]

        assert [alert["id"] for alert in poller.stream_new(timeout=0)] == [
            first_alert_id
        ]

        second = api.post_channel_line(
            f"[ {eve_chat_timestamp(1)} ] Scout B > Oijanen +1 reds",
            channel="Alliance Intel",
        )
        second_alert_id = second["alert"]["id"]

        assert [alert["id"] for alert in poller.stream_new(timeout=0)] == [
            second_alert_id
        ]
    finally:
        server.stop()


def test_intel_api_client_ignores_bootstrap_sse_and_yields_alert():
    api = IntelApiClient("http://example.invalid")
    body = (
        "id: bootstrap-1\n"
        "event: bootstrap\n"
        'data: {"schema_version": "intel_bootstrap.v1"}\n\n'
        ": keepalive\n\n"
        "id: evt-1\n"
        "event: alert\n"
        'data: {"id": "evt-1", "system_name": "S-KSWL", "names": ["Pilot"]}\n\n'
    )

    assert api._parse_alert_events(body) == [
        {"id": "evt-1", "system_name": "S-KSWL", "names": ["Pilot"]},
    ]


def test_alert_client_state_persists_recent_seen_ids(tmp_path):
    state_path = tmp_path / "alert_state.json"
    state = AlertClientState(state_path, max_seen_ids=2)

    assert state.load_seen_ids() == []
    assert state.loaded is False
    assert state.record_alert({"id": "evt-1"}) is True
    assert state.record_alert({"id": "evt-2"}) is True
    assert state.record_alert({"id": "evt-2"}) is False
    assert state.record_alert({"id": "evt-3"}) is True
    assert state.record_alert({"names": ["missing"]}) is False

    reloaded = AlertClientState(state_path, max_seen_ids=2)
    assert reloaded.load_seen_ids() == ["evt-2", "evt-3"]
    assert reloaded.loaded is True
    assert reloaded.has_seen("evt-2") is True


def test_alert_client_state_corruption_falls_back_to_empty(tmp_path):
    state_path = tmp_path / "alert_state.json"
    state_path.write_text("{bad json", encoding="utf-8")

    state = AlertClientState(state_path)

    assert state.load_seen_ids() == []
    assert state.loaded is True


def test_alert_event_consumer_deduplicates_before_ui(tmp_path):
    state = AlertClientState(tmp_path / "alert_state.json")
    state.load_seen_ids()
    consumer = AlertEventConsumer(state)

    assert consumer.accept({"id": "evt-1", "system_name": "S-KSWL"}) is True
    assert consumer.accept({"id": "evt-1", "system_name": "S-KSWL"}) is False
    assert consumer.accept({"system_name": "S-KSWL"}) is False


def test_alert_client_summarizes_location_and_enemy_count():
    assert summarize_alert(
        {
            "id": "evt-1",
            "system_name": "S-KSWL",
            "names": ["Pilot A", "Pilot B"],
            "created_at": "2026-07-10T00:00:00Z",
        }
    ) == {
        "id": "evt-1",
        "system_name": "S-KSWL",
        "hostile_count": 2,
        "created_at": "2026-07-10T00:00:00Z",
        "source_observation_id": "",
        "active_intel_id": "",
        "active": True,
    }
    assert alert_hostile_count({"metadata": {"hostile_count": 9}}) == 9


def test_alert_client_aggregates_overlay_rows_by_system():
    rows = aggregate_alert_summaries(
        [
            {"system_name": "S-KSWL", "hostile_count": 2, "created_at": "1"},
            {"system_name": "8-4GQM", "hostile_count": 1, "created_at": "2"},
            {"system_name": "S-KSWL", "hostile_count": 3, "created_at": "3"},
        ]
    )

    assert rows[0]["system_name"] == "S-KSWL"
    assert rows[0]["hostile_count"] == 3
    assert rows[0]["created_at"] == "1"
    assert rows[1]["system_name"] == "8-4GQM"


def test_alert_client_syncs_counts_and_drops_unmonitored_safe_systems():
    rows = sync_alert_summaries_from_bootstrap(
        [
            {"system_name": "S-KSWL", "hostile_count": 9, "created_at": "1"},
            {"system_name": "OLD", "hostile_count": 4, "created_at": "2"},
        ],
        {
            "map": {
                "systems": [
                    {"name": "8-4GQM", "hostile_count": 2},
                    {"name": "OLD", "hostile_count": 0},
                    {"name": "S-KSWL", "hostile_count": 3},
                ]
            },
            "active_intel": [
                {
                    "system_name": "S-KSWL",
                    "first_seen_at": "2026-07-22T10:00:00Z",
                    "active": True,
                },
                {
                    "system_name": "8-4GQM",
                    "first_seen_at": "2026-07-22T10:05:00Z",
                    "active": True,
                },
            ],
        },
        now=100.0,
    )

    assert [
        (item["system_name"], item["hostile_count"], item["active"])
        for item in rows
    ] == [
        ("S-KSWL", 3, True),
        ("8-4GQM", 2, True),
    ]

    rows = sync_alert_summaries_from_bootstrap(
        rows,
        {
            "map": {
                "systems": [
                    {"name": "S-KSWL", "hostile_count": 1},
                    {"name": "8-4GQM", "hostile_count": 0},
                ]
            },
            "active_intel": [],
        },
        now=110.0,
    )

    assert [(item["system_name"], item["active"]) for item in rows] == [
        ("S-KSWL", True),
    ]
    assert len(prune_inactive_alert_summaries(rows, now=159.9)) == 1
    rows = prune_inactive_alert_summaries(rows, now=170.0)
    assert [item["system_name"] for item in rows] == ["S-KSWL"]

    rows = sync_alert_summaries_from_bootstrap(
        [],
        {
            "map": {"systems": []},
            "alerts": [
                {"active_intel_id": "unmapped:1"},
                {"active_intel_id": "unmapped:2"},
            ],
            "active_intel": [
                {
                    "id": "unmapped:1",
                    "system_name": "UNMAPPED",
                    "first_seen_at": "2026-07-22T10:10:00Z",
                    "metadata": {"hostile_count": 2},
                    "active": True,
                },
                {
                    "id": "unmapped:2",
                    "system_name": "UNMAPPED",
                    "first_seen_at": "2026-07-22T10:11:00Z",
                    "metadata": {},
                    "active": True,
                },
            ],
        },
        now=200.0,
    )

    assert [(item["system_name"], item["hostile_count"]) for item in rows] == [
        ("UNMAPPED", 3)
    ]


def test_alert_client_merges_case_variant_monitoring_node_into_hostile_tile():
    rows = sync_alert_summaries_from_bootstrap(
        [
            {
                "system_name": "s-kswl",
                "hostile_count": 0,
                "active_hostile_count": 0,
                "active": False,
            }
        ],
        {
            "map": {
                "systems": [
                    {"name": "S-KSWL", "hostile_count": 3},
                ]
            },
            "active_intel": [],
            "clients": {
                "heartbeats": [
                    {
                        "client_type": "detector_client",
                        "online": True,
                        "details": {
                            "monitoring": True,
                            "system": "s-kswl",
                        },
                    }
                ]
            },
        },
    )

    assert rows == [
        {
            "system_name": "S-KSWL",
            "hostile_count": 3,
            "active_hostile_count": 3,
            "created_at": "",
            "active": True,
        }
    ]


def test_alert_summary_aggregation_merges_case_variant_system_names():
    rows = aggregate_alert_summaries(
        [
            {"system_name": "s-kswl", "hostile_count": 0, "active": False},
            {"system_name": "S-KSWL", "hostile_count": 2, "active": True},
        ]
    )

    assert rows == [
        {
            "system_name": "s-kswl",
            "hostile_count": 2,
            "active_hostile_count": 2,
            "created_at": "",
            "active": True,
        }
    ]


def test_alert_client_adds_green_tiles_for_online_monitoring_nodes():
    rows = sync_alert_summaries_from_bootstrap(
        [],
        {
            "map": {"systems": []},
            "active_intel": [],
            "clients": {
                "heartbeats": [
                    {
                        "client_type": "detector_client",
                        "online": True,
                        "details": {
                            "monitoring": True,
                            "system": "S-KSWL",
                            "targets": [
                                {
                                    "system_name": "S-KSWL",
                                    "monitoring": True,
                                }
                            ],
                        },
                    }
                ]
            },
        },
    )

    assert rows == [
        {
            "system_name": "S-KSWL",
            "hostile_count": 0,
            "active_hostile_count": 0,
            "created_at": "",
            "active": False,
        }
    ]


def test_alert_client_removes_green_tile_when_monitoring_node_stops():
    rows = sync_alert_summaries_from_bootstrap(
        [
            {
                "system_name": "S-KSWL",
                "hostile_count": 0,
                "active_hostile_count": 0,
                "created_at": "",
                "active": False,
            }
        ],
        {
            "map": {"systems": []},
            "active_intel": [],
            "clients": {
                "heartbeats": [
                    {
                        "client_type": "detector_client",
                        "online": True,
                        "details": {
                            "monitoring": False,
                            "system": "S-KSWL",
                        },
                    }
                ]
            },
        },
    )

    assert rows == []


def test_alert_client_marks_summaries_inactive_from_bootstrap():
    summaries = [
        {
            "id": "evt-active",
            "source_observation_id": "obs-active",
            "system_name": "S-KSWL",
            "hostile_count": 1,
            "created_at": "2026-07-10T00:00:00Z",
            "active": True,
        },
        {
            "id": "evt-left",
            "source_observation_id": "obs-left",
            "system_name": "5-O8B1",
            "hostile_count": 1,
            "created_at": "2026-07-10T00:01:00Z",
            "active": True,
        },
    ]
    active_keys = active_alert_keys_from_bootstrap(
        {"alerts": [{"id": "evt-active", "source_observation_id": "obs-active"}]}
    )

    updated = update_alert_summaries_active(summaries, active_keys)

    assert updated[0]["active"] is True
    assert updated[1]["active"] is False


def test_alert_client_formats_alert_time_as_local_clock():
    expected = datetime(2026, 7, 10, 3, 59, tzinfo=timezone.utc).astimezone()

    assert format_alert_time("2026-07-10T03:59:00+00:00") == expected.strftime("%H:%M")
    assert format_alert_time("") == ""
    assert format_alert_time("11:59") == "11:59"


def test_alert_overlay_renders_hostile_system_tile(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QScrollArea

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show_summaries(
            [
                {
                    "system_name": "S-KSWL",
                    "hostile_count": 99,
                    "created_at": "2026-07-10T00:00:00Z",
                }
            ]
        )
        app.processEvents()

        labels = {
            item.objectName(): item.text()
            for item in overlay.findChildren(QLabel)
            if item.objectName() in {"systemCell", "hostileCell", "stateCell"}
        }
        assert labels == {
            "systemCell": "S-KSWL",
            "hostileCell": "敌 99",
            "stateCell": "来敌",
        }
        system_cell = overlay.findChild(QLabel, "systemCell")
        hostile_cell = overlay.findChild(QLabel, "hostileCell")
        state_cell = overlay.findChild(QLabel, "stateCell")
        row = overlay.findChild(QFrame, "alertRow")
        scroll = overlay.findChild(QScrollArea, "alertScroll")
        assert system_cell is not None
        assert hostile_cell is not None
        assert state_cell is not None
        assert row is not None
        assert scroll is not None
        assert system_cell.minimumWidth() == overlay._tile_width - 18
        assert system_cell.maximumWidth() == overlay._tile_width - 18
        assert hostile_cell.width() == OVERLAY_HOSTILE_COUNT_WIDTH
        assert state_cell.x() == hostile_cell.x() + hostile_cell.width() + 4
        assert state_cell.x() <= row.width() * 3 // 5
        assert row.width() - (state_cell.x() + state_cell.width()) <= 20
        assert row.minimumWidth() == overlay._tile_width
        assert row.maximumWidth() == overlay._tile_width
        assert row.minimumHeight() == overlay._tile_height
        assert row.maximumHeight() == overlay._tile_height
        assert row.property("hostile") == "true"
        assert scroll.minimumHeight() == overlay._tile_height
        assert scroll.maximumHeight() == overlay._tile_height
        assert overlay.minimumWidth() == OVERLAY_MIN_WIDTH
        assert overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    finally:
        overlay.close()


def test_overlay_tile_dimensions_follow_available_screen_size():
    assert overlay_tile_dimensions(1366, 768) == (88, 58)
    assert overlay_tile_dimensions(1920, 1080) == (92, 62)
    assert overlay_tile_dimensions(3840, 2160) == (120, 74)


def test_alert_overlay_reflows_nodes_and_preserves_manual_size(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QFrame

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    summaries = [
        {"system_name": f"SYSTEM-{index}", "hostile_count": index}
        for index in range(6)
    ]
    try:
        overlay.show_summaries(summaries)
        overlay.show()
        overlay._user_resized = True
        overlay.resize(overlay.minimumWidth(), 250)
        app.processEvents()

        rows = [
            item
            for item in overlay.findChildren(QFrame)
            if item.objectName() == "alertRow" and item.isVisible()
        ]
        assert len({row.x() for row in rows}) == 1
        manual_size = overlay.size()

        overlay.show_summaries(summaries)
        app.processEvents()
        assert overlay.size() == manual_size

        three_column_width = (
            overlay._tile_width * 3 + OVERLAY_GRID_SPACING * 2 + 28
        )
        overlay.resize(three_column_width, 250)
        app.processEvents()

        assert len({row.x() for row in rows}) == 3
    finally:
        overlay.close()


def test_alert_overlay_resizes_from_bottom_right_corner(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QPoint, QRect
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show()
        overlay.move(100, 100)
        overlay.resize(220, 120)
        app.processEvents()
        start_geometry = QRect(overlay.geometry())
        start_position = overlay.frameGeometry().bottomRight()
        edges = overlay._resize_edges_at(start_position)

        assert edges == RESIZE_RIGHT | RESIZE_BOTTOM

        overlay._resize_edges = edges
        overlay._resize_start_geometry = start_geometry
        overlay._resize_start_position = start_position
        overlay._user_resized = True
        overlay._resize_from_pointer(start_position + QPoint(40, 30))
        app.processEvents()

        assert overlay.width() == start_geometry.width() + 40
        assert overlay.height() == start_geometry.height() + 30
    finally:
        overlay.close()


def test_alert_overlay_manual_resize_keeps_header_at_top(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtWidgets import QApplication

    class FakeResizePress:
        def __init__(self, global_x: int, global_y: int) -> None:
            self._position = QPointF(global_x, global_y)
            self.accepted = False

        def type(self):
            return QEvent.Type.MouseButtonPress

        def globalPosition(self):
            return self._position

        def button(self):
            return Qt.MouseButton.LeftButton

        def accept(self) -> None:
            self.accepted = True

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show()
        overlay.move(100, 100)
        app.processEvents()
        press = FakeResizePress(
            overlay.frameGeometry().right(),
            overlay.frameGeometry().bottom(),
        )

        assert overlay._handle_drag_event(press) is True
        assert press.accepted is True
        assert overlay._title.alignment() & Qt.AlignmentFlag.AlignTop
        assert overlay._status.alignment() & Qt.AlignmentFlag.AlignTop
    finally:
        overlay.close()


def test_alert_overlay_stays_on_a_left_hand_monitor(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QRect
    from PyQt6.QtWidgets import QApplication

    class FakeScreen:
        def availableGeometry(self):
            return QRect(-1920, 0, 1920, 1080)

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    overlay._screen_for_anchor = lambda: FakeScreen()
    try:
        overlay.show_summaries(
            [{"system_name": "S-KSWL", "hostile_count": 1}]
        )
        app.processEvents()

        assert -1920 <= overlay.x() < 0
        assert overlay.y() >= 0
    finally:
        overlay.close()


def test_alert_overlay_falls_back_to_hostile_count_for_legacy_summary(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show_summaries(
            [{"system_name": "S-KSWL", "hostile_count": 2, "active": True}]
        )
        app.processEvents()

        hostile_label = next(
            item
            for item in overlay.findChildren(QLabel)
            if item.objectName() == "hostileCell"
        )
        assert hostile_label.text() == "敌 2"
    finally:
        overlay.close()


def test_alert_overlay_expands_when_first_tile_arrives_after_show(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QScrollArea

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show()
        app.processEvents()
        empty_height = overlay.height()

        overlay.show_summaries(
            [
                {
                    "system_name": "S-KSWL",
                    "hostile_count": 0,
                    "active_hostile_count": 0,
                    "active": False,
                }
            ]
        )
        app.processEvents()

        scroll = overlay.findChild(QScrollArea, "alertScroll")
        assert scroll is not None
        assert scroll.isVisible()
        assert scroll.height() == overlay._tile_height
        assert overlay.height() > empty_height
    finally:
        overlay.close()


def test_alert_overlay_shrinks_title_after_last_tile_disappears(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show()
        overlay.show_summaries(
            [{"system_name": "S-KSWL", "hostile_count": 1}]
        )
        app.processEvents()
        populated_height = overlay.height()

        overlay.show_summaries([])
        app.processEvents()

        assert overlay.height() < populated_height
        assert overlay.height() == max(
            overlay.minimumHeight(),
            overlay.sizeHint().height(),
        )
        assert abs(
            overlay._title.geometry().center().y()
            - overlay.rect().center().y()
        ) <= 1
    finally:
        overlay.close()


def test_alert_overlay_keeps_more_than_four_rows_scrollable(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QScrollArea

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show_summaries(
            [
                {
                    "system_name": f"SYSTEM-{index}",
                    "hostile_count": 1,
                    "created_at": f"2026-07-10T00:0{index}:00Z",
                    "active": index != 4,
                }
                for index in range(6)
            ]
        )
        app.processEvents()

        rows = [
            item
            for item in overlay.findChildren(QFrame)
            if item.objectName() == "alertRow"
        ]
        scroll = overlay.findChild(QScrollArea, "alertScroll")
        assert len(rows) == 6
        system_labels = [
            item.text()
            for item in overlay.findChildren(QLabel)
            if item.objectName() == "systemCell"
        ]
        assert system_labels == [
            "SYSTEM-0",
            "SYSTEM-1",
            "SYSTEM-2",
            "SYSTEM-3",
            "SYSTEM-4",
            "SYSTEM-5",
        ]
        assert rows[4].property("hostile") == "false"
        state_labels = [
            item.text()
            for item in overlay.findChildren(QLabel)
            if item.objectName() == "stateCell"
        ]
        assert state_labels[4] == "安全"
        assert scroll is not None
        assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert scroll.maximumHeight() <= 160
    finally:
        overlay.close()


def test_alert_overlay_can_drag_from_child_rows(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtWidgets import QApplication, QLabel

    class FakeMouseEvent:
        def __init__(self, event_type, global_x, global_y, button, buttons):
            self._type = event_type
            self._global_position = QPointF(global_x, global_y)
            self._button = button
            self._buttons = buttons
            self.accepted = False

        def type(self):
            return self._type

        def globalPosition(self):
            return self._global_position

        def button(self):
            return self._button

        def buttons(self):
            return self._buttons

        def accept(self):
            self.accepted = True

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show_summaries(
            [
                {
                    "system_name": "S-KSWL",
                    "hostile_count": 1,
                    "created_at": "2026-07-10T00:00:00Z",
                }
            ]
        )
        overlay.move(100, 100)
        app.processEvents()
        row = next(
            item
            for item in overlay.findChildren(QLabel)
            if item.objectName() == "systemCell"
        )

        press = FakeMouseEvent(
            QEvent.Type.MouseButtonPress,
            120,
            120,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
        move = FakeMouseEvent(
            QEvent.Type.MouseMove,
            160,
            170,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
        release = FakeMouseEvent(
            QEvent.Type.MouseButtonRelease,
            160,
            170,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )

        assert overlay.eventFilter(row, press) is True
        assert overlay.eventFilter(row, move) is True
        assert overlay.pos().x() == 140
        assert overlay.pos().y() == 150
        assert overlay.eventFilter(row, release) is True
        overlay.show_summaries(
            [
                {
                    "system_name": "S-KSWL",
                    "hostile_count": 2,
                    "created_at": "2026-07-10T00:01:00Z",
                }
            ]
        )
        assert overlay.pos().x() == 140
        assert overlay.pos().y() == 150
        assert press.accepted is True
        assert move.accepted is True
        assert release.accepted is True
    finally:
        overlay.close()


def test_alert_client_parse_args_supports_sse_overlay_mode():
    args = parse_args(
        [
            "--server",
            "http://example.invalid",
            "--state",
            "alerts.json",
            "--timeout",
            "12",
            "--heartbeat-interval",
            "15",
            "--reconnect-max-delay",
            "20",
            "--hidden",
        ]
    )

    assert args.server == "http://example.invalid"
    assert args.state == "alerts.json"
    assert args.timeout == 12
    assert args.heartbeat_interval == 15
    assert args.reconnect_max_delay == 20
    assert args.hidden is True


def test_embedded_alert_controller_uses_host_notification_without_second_tray(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    notifications = []
    args = SimpleNamespace(
        server="http://example.invalid",
        state=str(tmp_path / "alerts.json"),
        timeout=1.0,
        heartbeat_interval=5.0,
        reconnect_max_delay=1.0,
    )
    app = QApplication.instance() or QApplication([])
    controller = AlertTrayController(
        app,
        args,
        tray_enabled=False,
        notification_callback=lambda title, message: notifications.append(
            (title, message)
        ),
    )
    try:
        controller._notify("EVE Sentry Alert", "S-KSWL 敌:2")

        assert controller._tray is None
        assert notifications == [("EVE Sentry Alert", "S-KSWL 敌:2")]
    finally:
        controller.overlay.close()


def test_alert_client_heartbeat_details_are_events_overlay_only():
    details = build_heartbeat_details(
        "connected",
        client_version="test-version",
        host="test-host",
        last_success_at="2026-07-10T00:00:00Z",
    )

    assert details["mode"] == "events"
    assert details["transport"] == "events"
    assert details["popup"] is True
    assert details["overlay"] is True
    assert details["details"] is False
    assert details["last_action"] == "connected"
    assert details["client_version"] == "test-version"
    assert details["host"] == "test-host"
    assert details["last_success_at"] == "2026-07-10T00:00:00Z"


def test_intel_api_client_sends_api_key_for_json_and_identity_requests(monkeypatch):
    requests = []
    responses = iter(
        [
            {"user": {"user_id": "user-1"}},
            {"identity": {"verified": True, "permanent": True}},
            {"identity": {"accepted": True, "status": "queued", "pending": True}},
            {"clients": {"heartbeats": [], "summary": {}}},
        ]
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        requests.append(request)
        return Response(next(responses))

    monkeypatch.setattr("app.intel_client.urlopen", fake_urlopen)
    client = IntelApiClient("https://sentry.test", api_key="eve_secret")

    assert client.validate_api_key()["user_id"] == "user-1"
    assert client.verify_eve_characters(["Alice"])["permanent"] is True
    assert client.ensure_eve_character_check(
        ["Alice"], client_id="detector:test"
    )["pending"] is True
    assert client.client_status()["heartbeats"] == []
    assert [request.get_header("Authorization") for request in requests] == [
        "Bearer eve_secret",
        "Bearer eve_secret",
        "Bearer eve_secret",
        "Bearer eve_secret",
    ]
    assert requests[0].full_url.endswith("/api/v1/auth/me")
    assert requests[2].full_url.endswith("/api/v1/client/identity-checks")
    assert json.loads(requests[2].data.decode("utf-8"))["client_id"] == "detector:test"


def test_intel_api_client_allows_api_key_over_configured_http_server():
    client = IntelApiClient("http://114.132.167.239:8765", api_key="eve_secret")
    assert client.api_key == "eve_secret"
