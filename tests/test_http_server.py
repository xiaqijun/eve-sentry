import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.esi.session import ContactStanding
from app.esi.sso import EsiSsoError
from app.intel.enrichment import ThreatEnricher
from app.intel.config import IntelConfigStore
from app.intel.scoring import ScoringEngine
from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


def request_json(url, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=3) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body) if body else {}


def request_text(url):
    request = Request(url, headers={"Accept": "text/event-stream"})
    with urlopen(request, timeout=3) as response:
        return response.status, response.headers, response.read().decode("utf-8")


def test_health_and_cors_preflight(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/health")
        assert status == 200
        assert payload == {"ok": True}

        request = Request(f"{server.url}/api/intel", method="OPTIONS")
        with urlopen(request, timeout=3) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert "DELETE" in response.headers["Access-Control-Allow-Methods"]
    finally:
        server.stop()


def test_esi_status_reports_disabled_session(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/esi/status")
        assert status == 200
        assert payload == {"enabled": False, "authenticated": False}

        try:
            request_json(f"{server.url}/api/esi/session")
        except HTTPError as exc:
            assert exc.code == 404
            error = json.loads(exc.read().decode("utf-8"))
            assert "ESI session" in error["error"]
        else:
            raise AssertionError("expected HTTP 404")
    finally:
        server.stop()


def test_esi_session_routes_expose_status_and_snapshot(tmp_path):
    class FakeTokens:
        character_id = 123
        character_owner_hash = "owner-hash"
        scopes = ["esi-location.read_location.v1"]
        expires_at = 2000

        def is_expired(self):
            return False

    class FakeSnapshot:
        def to_dict(self):
            return {
                "character_id": 123,
                "character_owner_hash": "owner-hash",
                "scopes": ["esi-location.read_location.v1"],
                "location": {"solar_system_id": 30002813},
                "contacts": [{"contact_id": 456, "standing": -10}],
            }

    class FakeResolver:
        def system_profile(self, system_id):
            assert system_id == 30002813
            return {
                "system_id": 30002813,
                "name": "Tama",
                "security_status": 0.3,
            }

    class FakeSession:
        def __init__(self):
            self.load_calls = []
            self.snapshot_calls = []

        def load_tokens(self, refresh_if_needed=True):
            self.load_calls.append(refresh_if_needed)
            return FakeTokens()

        def snapshot(self, include_location=True, include_contacts=True):
            self.snapshot_calls.append((include_location, include_contacts))
            return FakeSnapshot()

    session = FakeSession()
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json", resolver=FakeResolver()),
        port=0,
        esi_session=session,
    )
    server.start()
    try:
        status, status_payload = request_json(f"{server.url}/api/esi/status")
        assert status == 200
        assert status_payload["enabled"] is True
        assert status_payload["authenticated"] is True
        assert status_payload["character_id"] == 123
        assert "access_token" not in status_payload
        assert "refresh_token" not in status_payload
        assert session.load_calls == [False]

        status, snapshot = request_json(
            f"{server.url}/api/esi/session?location=false&contacts=true"
        )
        assert status == 200
        assert snapshot["authenticated"] is True
        assert snapshot["snapshot"]["contacts"][0]["standing"] == -10
        assert session.snapshot_calls == [(False, True)]

        status, location_snapshot = request_json(
            f"{server.url}/api/esi/session?location=true&contacts=false"
        )
        assert status == 200
        assert location_snapshot["snapshot"]["location"]["solar_system_id"] == (
            30002813
        )
        assert location_snapshot["snapshot"]["location"]["solar_system_name"] == "Tama"
        assert location_snapshot["snapshot"]["location"]["solar_system"]["name"] == (
            "Tama"
        )
        assert session.snapshot_calls == [(False, True), (True, False)]
    finally:
        server.stop()


def test_esi_session_snapshot_reports_missing_token(tmp_path):
    class MissingTokenSession:
        def load_tokens(self, refresh_if_needed=True):
            raise EsiSsoError("no saved ESI token")

        def snapshot(self, include_location=True, include_contacts=True):
            raise EsiSsoError("no saved ESI token")

    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json"),
        port=0,
        esi_session=MissingTokenSession(),
    )
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/esi/status")
        assert status == 200
        assert payload["authenticated"] is False
        assert "no saved ESI token" in payload["error"]

        try:
            request_json(f"{server.url}/api/esi/session")
        except HTTPError as exc:
            assert exc.code == 401
            error = json.loads(exc.read().decode("utf-8"))
            assert "no saved ESI token" in error["error"]
        else:
            raise AssertionError("expected HTTP 401")
    finally:
        server.stop()


def test_index_page_serves_config_panel(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        request = Request(f"{server.url}/")
        with urlopen(request, timeout=3) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            assert "Scoring Config" in body
            assert "Manual Intel" in body
            assert 'id="tab-alerts"' in body
            assert "function renderAlerts()" in body
            assert "/api/config" in body
            assert "/api/observations" in body
            assert "data-alert-details" in body
            assert "/api/kill-activity/character" in body
            assert "/api/characters/by-name" in body
    finally:
        server.stop()


def test_create_query_and_delete_report(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/intel",
            method="POST",
            payload={
                "system": "Tama",
                "names": ["Alice", "Bob"],
                "source": "test",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )
        assert status == 201
        report_id = created["report"]["id"]
        assert created["observation"]["id"] == report_id
        assert created["alert"]["source_observation_id"] == report_id

        status, reports = request_json(
            f"{server.url}/api/reports?{urlencode({'system': 'tama', 'limit': '5'})}"
        )
        assert status == 200
        assert reports["count"] == 1
        assert reports["reports"][0]["id"] == report_id

        status, deleted = request_json(
            f"{server.url}/api/intel/{report_id}",
            method="DELETE",
        )
        assert status == 200
        assert deleted == {"ok": True, "id": report_id}

        status, snapshot = request_json(f"{server.url}/api/intel")
        assert status == 200
        assert snapshot["summary"]["report_count"] == 0
    finally:
        server.stop()


def test_public_lookup_routes_return_profiles_and_activity(tmp_path):
    class FakeResolver:
        def resolve_names(self, names):
            if names == ["Alice"]:
                return [
                    SimpleNamespace(
                        category="character",
                        entity_id=123,
                        name="Alice",
                    )
                ]
            if names == ["Tama"]:
                return [
                    SimpleNamespace(
                        category="solar_system",
                        entity_id=30002813,
                        name="Tama",
                    )
                ]
            return []

        def character_profile(self, character_id):
            assert character_id == 123
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_id": 456,
                "alliance_id": 789,
            }

        def system_profile(self, system_id):
            assert system_id == 30002813
            return {
                "system_id": 30002813,
                "name": "Tama",
                "security_status": 0.3,
            }

    class FakeKillboard:
        def character_recent(self, character_id):
            assert character_id == 123
            return [
                {
                    "killmail_id": 1,
                    "killmail_time": "2026-06-30T10:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 999, "ship_type_id": 111},
                    "attackers": [{"character_id": 123}],
                },
                {
                    "killmail_id": 2,
                    "killmail_time": "2026-06-30T11:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 123, "ship_type_id": 222},
                    "attackers": [{"character_id": 456}],
                },
            ]

        def system_recent(self, system_id):
            assert system_id == 30002813
            return [
                {
                    "killmail_id": 3,
                    "killmail_time": "2026-06-30T12:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 777, "ship_type_id": 333},
                    "attackers": [{"character_id": 123}],
                },
                {
                    "killmail_id": 4,
                    "killmail_time": "2026-06-30T13:00:00Z",
                    "solar_system_id": 30002814,
                    "victim": {"character_id": 888, "ship_type_id": 444},
                    "attackers": [{"character_id": 456}],
                },
            ]

        def corporation_recent(self, corporation_id):
            assert corporation_id == 456
            return [
                {
                    "killmail_id": 5,
                    "killmail_time": "2026-06-30T14:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 999, "corporation_id": 777},
                    "attackers": [{"character_id": 123, "corporation_id": 456}],
                },
                {
                    "killmail_id": 6,
                    "killmail_time": "2026-06-30T15:00:00Z",
                    "solar_system_id": 30002814,
                    "victim": {"character_id": 456, "corporation_id": 456},
                    "attackers": [{"character_id": 888, "corporation_id": 777}],
                },
            ]

        def alliance_recent(self, alliance_id):
            assert alliance_id == 789
            return [
                {
                    "killmail_id": 7,
                    "killmail_time": "2026-06-30T16:00:00Z",
                    "solar_system_id": 30002815,
                    "victim": {"character_id": 555, "alliance_id": 789},
                    "attackers": [{"character_id": 123, "alliance_id": 111}],
                }
            ]

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel.json",
        resolver=resolver,
        enricher=ThreatEnricher(
            resolver=resolver,
            killboard=FakeKillboard(),
            kill_window="7d",
        ),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, by_name = request_json(f"{server.url}/api/characters/by-name/Alice")
        assert status == 200
        assert by_name["character"]["character_id"] == 123
        assert by_name["character"]["corporation_id"] == 456

        status, by_id = request_json(f"{server.url}/api/characters/123")
        assert status == 200
        assert by_id["character"]["alliance_id"] == 789

        status, system = request_json(f"{server.url}/api/systems/by-name/Tama")
        assert status == 200
        assert system["system"]["system_id"] == 30002813
        assert system["system"]["security_status"] == 0.3

        status, system = request_json(f"{server.url}/api/systems/30002813")
        assert status == 200
        assert system["system"]["name"] == "Tama"
        assert system["system"]["system_id"] == 30002813

        status, character_activity = request_json(
            f"{server.url}/api/kill-activity/character/123"
        )
        assert status == 200
        assert character_activity["activity"]["character_id"] == 123
        assert character_activity["activity"]["kills"] == 1
        assert character_activity["activity"]["losses"] == 1

        status, system_activity = request_json(
            f"{server.url}/api/kill-activity/system/30002813"
        )
        assert status == 200
        assert system_activity["activity"]["system_id"] == 30002813
        assert system_activity["activity"]["kills"] == 1
        assert system_activity["activity"]["character_ids"] == [123, 777]

        status, corporation_activity = request_json(
            f"{server.url}/api/kill-activity/corporation/456"
        )
        assert status == 200
        assert corporation_activity["activity"]["corporation_id"] == 456
        assert corporation_activity["activity"]["kills"] == 1
        assert corporation_activity["activity"]["losses"] == 1

        status, alliance_activity = request_json(
            f"{server.url}/api/kill-activity/alliance/789"
        )
        assert status == 200
        assert alliance_activity["activity"]["alliance_id"] == 789
        assert alliance_activity["activity"]["kills"] == 0
        assert alliance_activity["activity"]["losses"] == 1
    finally:
        server.stop()


def test_public_lookup_routes_report_disabled_sources(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        try:
            request_json(f"{server.url}/api/characters/123")
        except HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read().decode("utf-8"))
            assert "ESI" in payload["error"]
        else:
            raise AssertionError("expected HTTP 404")

        try:
            request_json(f"{server.url}/api/kill-activity/character/123")
        except HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read().decode("utf-8"))
            assert "killboard" in payload["error"]
        else:
            raise AssertionError("expected HTTP 404")

        try:
            request_json(f"{server.url}/api/kill-activity/corporation/456")
        except HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read().decode("utf-8"))
            assert "killboard" in payload["error"]
        else:
            raise AssertionError("expected HTTP 404")

        try:
            request_json(f"{server.url}/api/kill-activity/character/not-a-number")
        except HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "character_id" in payload["error"]
        else:
            raise AssertionError("expected HTTP 400")

        try:
            request_json(f"{server.url}/api/kill-activity/alliance/not-a-number")
        except HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "alliance_id" in payload["error"]
        else:
            raise AssertionError("expected HTTP 400")
    finally:
        server.stop()


def test_authenticated_standings_contribute_to_alert_scoring(tmp_path):
    class FakeResolver:
        def character_profile(self, character_id):
            assert character_id == 123
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_id": 456,
            }

    class FakeSession:
        def snapshot(self, include_location=True, include_contacts=True):
            return SimpleNamespace(
                contacts=[
                    ContactStanding(
                        contact_id=456,
                        contact_type="corporation",
                        standing=-10,
                    )
                ]
            )

    store = IntelStore(
        tmp_path / "intel.json",
        scorer=ScoringEngine(cooldown_seconds=0),
        enricher=ThreatEnricher(
            resolver=FakeResolver(),
            esi_session=FakeSession(),
        ),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "source": "local_ocr",
                "system_name": "Tama",
                "names": ["Alice"],
                "character_ids": [123],
                "seen_at": "2026-06-30T12:00:00+00:00",
            },
        )

        assert status == 201
        evidence_types = {item["type"] for item in created["alert"]["evidence"]}
        assert "hostile_standing" in evidence_types
        assert created["alert"]["score"] == 110

        status, payload = request_json(
            f"{server.url}/api/alerts/{created['alert']['id']}"
        )
        assert status == 200
        profile = payload["detail"]["context"]["character_profiles"][0]
        assert profile["contact_standing"] == -10.0
        assert "Hostile standing -10" in payload["detail"]["explanation"]["reasons"]
        assert "ESI profile Alice: corp 456, standing -10" in (
            payload["detail"]["explanation"]["context"]
        )
    finally:
        server.stop()


def test_alert_detail_route_returns_explanation_context(tmp_path):
    class FakeResolver:
        def character_profile(self, character_id):
            assert character_id == 123
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_id": 456,
                "alliance_id": 789,
            }

    class FakeKillboard:
        def character_recent(self, character_id):
            assert character_id == 123
            return [
                {
                    "killmail_id": 1,
                    "killmail_time": "2026-06-30T10:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 999, "ship_type_id": 111},
                    "attackers": [{"character_id": 123}],
                }
            ]

        def corporation_recent(self, corporation_id):
            assert corporation_id == 456
            return [
                {
                    "killmail_id": 2,
                    "killmail_time": "2026-06-30T11:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 999, "corporation_id": 777},
                    "attackers": [{"character_id": 123, "corporation_id": 456}],
                }
            ]

        def alliance_recent(self, alliance_id):
            assert alliance_id == 789
            return [
                {
                    "killmail_id": 3,
                    "killmail_time": "2026-06-30T12:00:00Z",
                    "solar_system_id": 30002813,
                    "victim": {"character_id": 888, "alliance_id": 789},
                    "attackers": [{"character_id": 123, "alliance_id": 111}],
                }
            ]

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
        enricher=ThreatEnricher(
            resolver=resolver,
            killboard=FakeKillboard(),
            kill_window="7d",
        ),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "source": "intel_channel",
                "source_instance": "Alliance Intel",
                "system_name": "Tama",
                "raw_text": "Scout A: Tama +3 reds",
                "seen_at": "2026-06-30T11:58:00+00:00",
            },
        )
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "source": "local_ocr",
                "system_name": "Tama",
                "names": ["Alice"],
                "character_ids": [123],
                "seen_at": "2026-06-30T12:00:00+00:00",
            },
        )
        assert status == 201

        status, payload = request_json(
            f"{server.url}/api/alerts/{created['alert']['id']}"
        )

        assert status == 200
        detail = payload["detail"]
        assert detail["alert"]["id"] == created["alert"]["id"]
        assert detail["observation"]["id"] == created["observation"]["id"]
        assert detail["context"]["channel_mentions"][0]["relation"] == "same_system"
        assert detail["context"]["character_profiles"][0]["character_id"] == 123
        assert detail["context"]["kill_activities"][0]["character_id"] == 123
        assert {
            item["entity_type"]
            for item in detail["context"]["group_activities"]
        } == {"corporation", "alliance"}
        assert detail["explanation"]["summary"].startswith(
            "HIGH alert for Alice in Tama"
        )
        assert "scoring" in detail["explanation"]["sources"]
        assert "enrichment" in detail["explanation"]["sources"]
        assert "Local OCR saw Alice in Tama" in detail["explanation"]["reasons"]
        assert (
            "Recent channel same-system mention in Tama 2m ago"
            in detail["explanation"]["context"]
        )
        assert "ESI profile Alice: corp 456, alliance 789" in (
            detail["explanation"]["context"]
        )
        assert "Character 123 has 1 kill in 7d" in detail["explanation"]["context"]

        try:
            request_json(f"{server.url}/api/alerts/missing")
        except HTTPError as exc:
            assert exc.code == 404
            error = json.loads(exc.read().decode("utf-8"))
            assert "alert" in error["error"]
        else:
            raise AssertionError("expected HTTP 404")
    finally:
        server.stop()


def test_create_observation_and_query_alerts(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "raw_text": "Tama Alice",
                "metadata": {"hostile_count": 1, "sender": "Scout A"},
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )
        assert status == 201
        observation_id = created["observation"]["id"]
        assert created["alert"]["id"] == f"evt_{observation_id}"

        status, observations = request_json(
            f"{server.url}/api/observations?{urlencode({'source': 'intel_channel'})}"
        )
        assert status == 200
        assert observations["count"] == 1
        assert observations["observations"][0]["raw_text"] == "Tama Alice"
        assert observations["observations"][0]["metadata"]["sender"] == "Scout A"

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 1
        assert alerts["alerts"][0]["score"] == 30
    finally:
        server.stop()


def test_create_observation_is_idempotent_for_same_channel_line(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        payload = {
            "system_name": "Tama",
            "names": [],
            "source": "intel_channel",
            "source_instance": "Alliance Intel",
            "raw_text": "Scout A: Tama +3 reds",
            "metadata": {"hostile_count": 3, "sender": "Scout A"},
            "seen_at": "2026-06-29T12:00:00+00:00",
        }
        status, first = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload=payload,
        )
        status2, second = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={**payload, "id": "duplicate-id"},
        )

        assert status == 201
        assert status2 == 201
        assert second["observation"]["id"] == first["observation"]["id"]
        assert second["alert"]["id"] == first["alert"]["id"]

        status, observations = request_json(f"{server.url}/api/observations")
        assert status == 200
        assert observations["count"] == 1

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 1
    finally:
        server.stop()


def test_create_channel_line_parses_and_deduplicates(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        payload = {
            "channel": "Alliance Intel",
            "line": "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
        }
        status, first = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload=payload,
        )
        status2, second = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload=payload,
        )

        assert status == 201
        assert status2 == 201
        assert first["ignored"] is False
        assert first["parsed"]["system_name"] == "Tama"
        assert first["parsed"]["metadata"]["hostile_count"] == 3
        assert first["parsed"]["metadata"]["raw_line"] == payload["line"]
        assert first["observation"]["source_instance"] == "Alliance Intel"
        assert first["observation"]["metadata"]["sender"] == "Scout A"
        assert first["alert"]["source_observation_id"] == first["observation"]["id"]
        assert second["observation"]["id"] == first["observation"]["id"]
        assert second["alert"]["id"] == first["alert"]["id"]

        status, observations = request_json(f"{server.url}/api/observations")
        assert status == 200
        assert observations["count"] == 1
    finally:
        server.stop()


def test_create_channel_line_ignores_non_chat_headers(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, ignored = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={"channel": "Alliance Intel", "line": "Listener: Alliance Intel"},
        )

        assert status == 200
        assert ignored == {
            "ok": True,
            "ignored": True,
            "reason": "not a chat message",
        }

        status, observations = request_json(f"{server.url}/api/observations")
        assert status == 200
        assert observations["count"] == 0
    finally:
        server.stop()


def test_ack_alert_route_marks_alert(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )
        assert status == 201
        alert_id = created["alert"]["id"]

        status, acked = request_json(
            f"{server.url}/api/alerts/{alert_id}/ack",
            method="POST",
            payload={"acknowledged_by": "tester", "note": "handled"},
        )

        assert status == 200
        assert acked["ok"] is True
        assert acked["alert"]["id"] == alert_id
        assert acked["alert"]["acknowledged"] is True
        assert acked["alert"]["acknowledged_by"] == "tester"
        assert acked["alert"]["acknowledgement_note"] == "handled"

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["alerts"][0]["acknowledged"] is True

        try:
            request_json(
                f"{server.url}/api/alerts/missing/ack",
                method="POST",
                payload={},
            )
        except HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read().decode("utf-8"))
            assert "alert" in payload["error"]
        else:
            raise AssertionError("expected HTTP 404")
    finally:
        server.stop()


def test_alert_route_filters_by_acknowledgement_score_and_level(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        _, low_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Scout"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )
        _, medium_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "local_ocr",
                "seen_at": "2026-06-29T12:01:00+00:00",
            },
        )

        status, default_alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert default_alerts["count"] == 2

        request_json(
            f"{server.url}/api/alerts/{medium_created['alert']['id']}/ack",
            method="POST",
            payload={"acknowledged_by": "tester"},
        )

        status, unacknowledged = request_json(
            f"{server.url}/api/alerts?{urlencode({'acknowledged': 'false'})}"
        )
        assert status == 200
        assert [alert["id"] for alert in unacknowledged["alerts"]] == [
            low_created["alert"]["id"]
        ]

        status, acknowledged = request_json(
            f"{server.url}/api/alerts?{urlencode({'acknowledged': 'true'})}"
        )
        assert status == 200
        assert [alert["id"] for alert in acknowledged["alerts"]] == [
            medium_created["alert"]["id"]
        ]

        status, min_score = request_json(
            f"{server.url}/api/alerts?{urlencode({'min_score': '40'})}"
        )
        assert status == 200
        assert [alert["id"] for alert in min_score["alerts"]] == [
            medium_created["alert"]["id"]
        ]

        status, min_level = request_json(
            f"{server.url}/api/alerts?{urlencode({'min_level': 'medium'})}"
        )
        assert status == 200
        assert [alert["id"] for alert in min_level["alerts"]] == [
            medium_created["alert"]["id"]
        ]

        for query in (
            {"acknowledged": "maybe"},
            {"min_score": "-1"},
            {"min_level": "urgent"},
        ):
            try:
                request_json(f"{server.url}/api/alerts?{urlencode(query)}")
            except HTTPError as exc:
                assert exc.code == 400
            else:
                raise AssertionError("expected HTTP 400")
    finally:
        server.stop()


def test_events_stream_returns_alert_sse(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )
        assert status == 201

        status, headers, body = request_text(
            f"{server.url}/api/events?{urlencode({'timeout': '0', 'limit': '5'})}"
        )

        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "event: alert" in body
        data_line = next(line for line in body.splitlines() if line.startswith("data:"))
        payload = json.loads(data_line[len("data:"):].strip())
        assert payload["id"] == created["alert"]["id"]
    finally:
        server.stop()


def test_events_stream_applies_alert_filters(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        _, low_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Scout"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )
        _, medium_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "local_ocr",
                "seen_at": "2026-06-29T12:01:00+00:00",
            },
        )

        status, _, body = request_text(
            f"{server.url}/api/events?"
            f"{urlencode({'timeout': '0', 'min_level': 'medium'})}"
        )

        assert status == 200
        assert medium_created["alert"]["id"] in body
        assert low_created["alert"]["id"] not in body
    finally:
        server.stop()


def test_config_api_updates_scoring_rules_and_clears_cached_alerts(tmp_path):
    config_store = IntelConfigStore(tmp_path / "intel_config.json")
    store = IntelStore(
        tmp_path / "intel.json",
        scorer=config_store.build_scorer(),
    )
    server = IntelHTTPServer(store, port=0, config_store=config_store)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
            },
        )
        assert status == 201
        assert created["alert"]["score"] == 30

        status, updated = request_json(
            f"{server.url}/api/config",
            method="PUT",
            payload={"whitelist": ["Alice"]},
        )
        assert status == 200
        assert updated["config"]["whitelist"] == ["Alice"]

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts == {"alerts": [], "count": 0}

        status, suppressed = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
            },
        )
        assert status == 201
        assert suppressed["alert"] is None

        status, config = request_json(f"{server.url}/api/config")
        assert status == 200
        assert config["config"]["whitelist"] == ["Alice"]
    finally:
        server.stop()


def test_config_api_rejects_invalid_payload(tmp_path):
    config_store = IntelConfigStore(tmp_path / "intel_config.json")
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json", scorer=config_store.build_scorer()),
        port=0,
        config_store=config_store,
    )
    server.start()
    try:
        try:
            request_json(
                f"{server.url}/api/config",
                method="PUT",
                payload={"cooldown_seconds": -1},
            )
        except HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "cooldown_seconds" in payload["error"]
        else:
            raise AssertionError("expected HTTP 400")
    finally:
        server.stop()


def test_invalid_post_returns_400(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        try:
            request_json(
                f"{server.url}/api/intel",
                method="POST",
                payload={"system": "Tama", "names": []},
            )
        except HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8"))
            assert "names" in payload["error"]
        else:
            raise AssertionError("expected HTTP 400")
    finally:
        server.stop()
