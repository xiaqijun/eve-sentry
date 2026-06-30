import io
import json
from types import SimpleNamespace

from app.alert_client import (
    AlertClientState,
    AlertStreamFallback,
    ack_emitted_alerts,
    attach_alert_details,
    build_popup_names,
    emit_alerts,
    format_alert,
    format_report,
    parse_args,
    run_alert_client,
)
from app.intel_client import AlertPoller, IntelApiClient, IntelApiError, ReportPoller
from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


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


def test_intel_api_client_filters_alerts(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)

        low = api.post_observation(
            system_name="Tama",
            names=["Scout"],
            source="intel_channel",
            seen_at="2026-06-29T12:00:00+00:00",
        )
        medium = api.post_observation(
            system_name="Tama",
            names=["Alice"],
            source="local_ocr",
            seen_at="2026-06-29T12:01:00+00:00",
        )
        api.ack_alert(medium["alert"]["id"], acknowledged_by="client")

        assert [item["id"] for item in api.list_alerts(acknowledged=False)] == [
            low["alert"]["id"]
        ]
        assert [item["id"] for item in api.list_alerts(acknowledged=True)] == [
            medium["alert"]["id"]
        ]
        assert [item["id"] for item in api.list_alerts(min_score=40)] == [
            medium["alert"]["id"]
        ]
        assert [item["id"] for item in api.stream_alerts(
            timeout=0,
            min_level="medium",
        )] == [medium["alert"]["id"]]
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
            "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
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

        assert detail["alert"]["id"] == created["alert"]["id"]
        assert detail["observation"]["id"] == created["observation"]["id"]
        assert detail["context"]["channel_mentions"] == []
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

        first = api.post_observation(
            system_name="Tama",
            names=["Alice"],
            source="intel_channel",
            seen_at="2026-06-29T12:00:00+00:00",
            metadata={"batch": "resume"},
        )
        first_alert_id = first["alert"]["id"]
        first_created_at = first["alert"]["created_at"]

        assert [alert["id"] for alert in poller.stream_new(timeout=0)] == [
            first_alert_id
        ]

        second = api.post_observation(
            system_name="Tama",
            names=["Bob"],
            source="intel_channel",
            seen_at="2026-06-29T12:01:00+00:00",
            received_at=first_created_at,
            metadata={"batch": "resume"},
        )
        second_alert_id = second["alert"]["id"]

        assert [alert["id"] for alert in poller.stream_new(timeout=0)] == [
            second_alert_id
        ]
    finally:
        server.stop()


def test_alert_client_state_persists_recent_seen_ids(tmp_path):
    state_path = tmp_path / "alert_state.json"
    state = AlertClientState(state_path, max_seen_ids=2)

    assert state.load_seen_ids() == []
    assert state.loaded is False

    state.record_alerts(
        [
            {"id": "evt-1"},
            {"id": "evt-2"},
            {"id": "evt-2"},
            {"id": "evt-3"},
            {"names": ["missing"]},
        ]
    )

    reloaded = AlertClientState(state_path, max_seen_ids=2)
    assert reloaded.load_seen_ids() == ["evt-2", "evt-3"]
    assert reloaded.loaded is True


def test_alert_client_state_corruption_falls_back_to_empty(tmp_path):
    state_path = tmp_path / "alert_state.json"
    state_path.write_text("{bad json", encoding="utf-8")

    state = AlertClientState(state_path)

    assert state.load_seen_ids() == []
    assert state.loaded is True


def test_alert_poller_passes_server_side_filters_to_polling_and_streaming():
    api = FakeApi(
        [
            [{"id": "poll", "created_at": "1", "names": ["Alice"]}],
            [{"id": "stream", "created_at": "2", "names": ["Bob"]}],
        ]
    )
    poller = AlertPoller(
        api,
        acknowledged=False,
        min_score=70,
        min_level="high",
    )

    assert [alert["id"] for alert in poller.poll_new()] == ["poll"]
    assert [alert["id"] for alert in poller.stream_new(timeout=0)] == ["stream"]
    assert api.alert_filters == [
        (False, 70, "high"),
        (False, 70, "high"),
    ]


def test_alert_stream_fallback_retries_stream_after_poll_and_cooldown():
    now = 100.0
    fallback = AlertStreamFallback(
        enabled=True,
        retry_interval=5.0,
        clock=lambda: now,
    )

    assert fallback.should_stream() is True

    fallback.mark_stream_failure()

    assert fallback.should_stream() is False

    fallback.mark_poll_attempt()

    assert fallback.should_stream() is False

    now = 105.0

    assert fallback.should_stream() is True

    fallback.mark_stream_success()

    now = 106.0

    assert fallback.should_stream() is True


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

    detailed_alert = {
        "system_name": "Tama",
        "names": ["Alice"],
        "created_at": "2026-06-29T12:00:00+00:00",
        "level": "high",
        "score": 85,
        "evidence": [
            {"type": "local_ocr_seen", "summary": "Local OCR saw Alice"},
        ],
        "detail": {
            "context": {
                "channel_mentions": [
                    {
                        "relation": "same_system",
                        "observation": {"system_name": "Tama"},
                    }
                ],
                "character_profiles": [
                    {
                        "character_id": 123,
                        "name": "Alice",
                        "corporation_id": 456,
                    }
                ],
                "kill_activities": [
                    {"character_id": 123, "kills": 2, "losses": 1}
                ],
                "group_activities": [
                    {
                        "entity_type": "corporation",
                        "entity_id": 456,
                        "kills": 5,
                    }
                ],
            }
        },
    }
    assert (
        format_alert(detailed_alert)
        == "HIGH 2026-06-29T12:00:00+00:00 Tama: Alice (score 85) "
        "- Local OCR saw Alice | Context: channel same-system Tama; "
        "profile Alice (corp 456); character 123 2 kills, 1 loss; "
        "corporation 456 5 kills"
    )

    detailed_alert["detail"]["explanation"] = {
        "context": [
            "Recent channel same-system mention in Tama 2m ago",
            "ESI profile Alice: corp 456",
            "Character 123 has 2 kills, 1 loss in 7d",
        ]
    }
    assert (
        format_alert(detailed_alert)
        == "HIGH 2026-06-29T12:00:00+00:00 Tama: Alice (score 85) "
        "- Local OCR saw Alice | Context: Recent channel same-system mention "
        "in Tama 2m ago; ESI profile Alice: corp 456; Character 123 has "
        "2 kills, 1 loss in 7d"
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
            "--details",
            "--unacknowledged-only",
            "--min-score",
            "70",
            "--min-level",
            "high",
            "--stream-retry-interval",
            "15",
            "--state",
            "alerts.json",
            "--no-state",
        ]
    )

    assert args.once is True
    assert args.json is True
    assert args.poll is True
    assert args.ignore_existing is False
    assert args.ack is True
    assert args.ack_by == "cli"
    assert args.ack_note == "handled"
    assert args.details is True
    assert args.unacknowledged_only is True
    assert args.min_score == 70
    assert args.min_level == "high"
    assert args.stream_retry_interval == 15
    assert args.state == "alerts.json"
    assert args.no_state is True


def test_alert_client_once_falls_back_to_polling_when_stream_fails(
    monkeypatch,
    capsys,
):
    class StreamingFailsApi:
        instances = []

        def __init__(self, base_url, timeout=3.0):
            self.base_url = base_url
            self.timeout = timeout
            self.poll_calls = 0
            self.stream_calls = 0
            self.instances.append(self)

        def list_alerts(
            self,
            limit=50,
            acknowledged=None,
            min_score=None,
            min_level="",
        ):
            self.poll_calls += 1
            return [
                {
                    "id": "evt-poll",
                    "system_name": "Tama",
                    "names": ["Fallback"],
                    "created_at": "2026-06-29T12:00:00+00:00",
                    "level": "high",
                    "score": 70,
                }
            ]

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
            _ = since, last_event_id
            self.stream_calls += 1
            raise IntelApiError("stream offline")

    monkeypatch.setattr("app.alert_client.IntelApiClient", StreamingFailsApi)

    args = parse_args(
        [
            "--server",
            "http://example.invalid",
            "--once",
            "--include-existing",
            "--no-state",
        ]
    )

    assert run_alert_client(args) == 0

    output = capsys.readouterr()
    api = StreamingFailsApi.instances[0]

    assert api.stream_calls == 1
    assert api.poll_calls == 1
    assert "Fallback" in output.out


def test_alert_client_resume_state_preserves_offline_alerts(tmp_path, capsys):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        api = IntelApiClient(server.url)
        old = api.post_observation(
            system_name="Tama",
            names=["Old"],
            source="intel_channel",
            seen_at="2026-06-29T12:00:00+00:00",
        )
        state_path = tmp_path / "alert_state.json"

        first_args = parse_args(
            [
                "--server",
                server.url,
                "--once",
                "--poll",
                "--state",
                str(state_path),
            ]
        )
        assert run_alert_client(first_args) == 0
        first_output = capsys.readouterr()
        assert "[ALERT]" not in first_output.out
        assert AlertClientState(state_path).load_seen_ids() == [old["alert"]["id"]]

        new = api.post_observation(
            system_name="Tama",
            names=["New"],
            source="intel_channel",
            seen_at="2026-06-29T12:01:00+00:00",
        )

        second_args = parse_args(
            [
                "--server",
                server.url,
                "--once",
                "--poll",
                "--state",
                str(state_path),
            ]
        )
        assert run_alert_client(second_args) == 0
        second_output = capsys.readouterr()

        assert old["alert"]["id"] not in second_output.out
        assert new["alert"]["id"] not in second_output.out
        assert "New" in second_output.out
        assert AlertClientState(state_path).load_seen_ids() == [
            old["alert"]["id"],
            new["alert"]["id"],
        ]
    finally:
        server.stop()


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


def test_alert_client_attaches_alert_details_without_blocking_delivery():
    class DetailApi:
        def __init__(self):
            self.calls = []

        def alert_detail(self, alert_id):
            self.calls.append(alert_id)
            if alert_id == "bad":
                raise IntelApiError("detail offline")
            return {
                "alert": {"id": alert_id},
                "observation": {"id": "obs-1"},
                "context": {"channel_mentions": []},
            }

    api = DetailApi()

    alerts = attach_alert_details(
        api,
        [{"id": "evt-1", "names": ["Alice"]}, {"id": "bad"}, {"names": ["No id"]}],
    )

    assert api.calls == ["evt-1", "bad"]
    assert alerts[0]["detail"]["alert"]["id"] == "evt-1"
    assert alerts[1]["detail_error"] == "detail offline"
    assert "detail" not in alerts[2]


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
