import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.alert_client import (
    AlertClientState,
    AlertEventConsumer,
    AlertOverlay,
    active_alert_keys_from_bootstrap,
    aggregate_alert_summaries,
    alert_hostile_count,
    build_heartbeat_details,
    format_alert_time,
    parse_args,
    summarize_alert,
    update_alert_summaries_active,
)
from app.intel_client import AlertPoller, IntelApiClient, ReportPoller
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
        acknowledged=None,
        min_score=None,
        min_level="",
    ):
        self.alert_filters.append((acknowledged, min_score, min_level))
        return self.list_reports(limit=limit)

    def stream_alerts(
        self,
        since="",
        last_event_id="",
        limit=50,
        timeout=30.0,
        acknowledged=None,
        min_score=None,
        min_level="",
    ):
        _ = timeout
        self.stream_since.append(since)
        self.stream_last_event_ids.append(last_event_id)
        self.alert_filters.append((acknowledged, min_score, min_level))
        return self.list_reports(limit=limit)

    def iter_alert_events(self, **kwargs):
        for alert in self.stream_alerts(**kwargs):
            yield alert

    def ack_alert(self, alert_id, acknowledged_by="", note=""):
        _ = alert_id, acknowledged_by, note
        return {}


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
        if "/alerts/" in path and path.endswith("/ack"):
            return {"alert": {"id": "evt-1"}}
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
    api.ack_alert("evt-1")
    api.bootstrap()
    api.map_snapshot()
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
        "/api/v1/alerts/evt-1/ack",
        "/api/v1/bootstrap",
        "/api/v1/map",
        "/api/v1/map/systems/30002813",
    ]


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

        acked = api.ack_alert(
            f"evt_{observation_id}",
            acknowledged_by="client",
            note="handled",
        )
        assert acked["acknowledged"] is True
        assert acked["acknowledged_by"] == "client"
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


def test_intel_api_client_filters_alerts(tmp_path):
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
        api.ack_alert(medium["alert"]["id"], acknowledged_by="client")

        assert [item["id"] for item in api.list_alerts(acknowledged=False)] == [
            low["alert"]["id"]
        ]
        assert [item["id"] for item in api.list_alerts(acknowledged=True)] == [
            medium["alert"]["id"]
        ]
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
    assert rows[0]["hostile_count"] == 5
    assert rows[0]["created_at"] == "1"
    assert rows[1]["system_name"] == "8-4GQM"


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


def test_alert_overlay_can_render_compact_enemy_rows(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    overlay = AlertOverlay()
    try:
        overlay.show_summaries(
            [
                {
                    "system_name": "S-KSWL",
                    "hostile_count": 9,
                    "created_at": "2026-07-10T00:00:00Z",
                }
            ]
        )
        app.processEvents()

        labels = {
            item.objectName(): item.text()
            for item in overlay.findChildren(QLabel)
            if item.objectName() in {"systemCell", "hostileCell", "timeCell"}
        }
        expected_time = format_alert_time("2026-07-10T00:00:00Z")
        assert labels == {
            "systemCell": "S-KSWL",
            "hostileCell": "9",
            "timeCell": expected_time,
        }
        assert overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
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
        assert rows[4].property("inactive") == "true"
        system_labels = [
            item.text()
            for item in overlay.findChildren(QLabel)
            if item.objectName() == "systemCell"
        ]
        assert system_labels == [f"SYSTEM-{index}" for index in range(6)]
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
