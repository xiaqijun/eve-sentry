import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.core.models import Evidence, ThreatEvent
from app.esi.cache import EsiCache
from app.esi.resolver import EsiResolver
from app.esi.session import ContactStanding
from app.esi.sso import EsiSsoError
from app.intel.classification import CLASSIFICATION_VERSION
from app.intel.enrichment import ThreatEnricher
from app.intel.config import IntelConfigStore
from app.intel.scoring import ScoringEngine, Watchlist
from app.server.http_server import IntelHTTPServer, IntelRequestHandler
from app.server.intel_store import IntelStore, StarSystem
from app.server.map_config import MapConfigStore
from app.server.sqlite_store import SQLiteIntelStore


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


def request_text(url, headers=None, timeout=3):
    request_headers = {"Accept": "text/event-stream"}
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return response.status, response.headers, response.read().decode("utf-8")


def sse_events(body):
    events = []
    for chunk in body.split("\n\n"):
        event = {}
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event["event"] = line[len("event:"):].strip()
            elif line.startswith("id:"):
                event["id"] = line[len("id:"):].strip()
            elif line.startswith("data:"):
                event["data"] = json.loads(line[len("data:"):].strip())
        if event:
            events.append(event)
    return events


def write_sde_fixture(root):
    bsd_dir = root / "bsd"
    bsd_dir.mkdir(parents=True)
    (bsd_dir / "mapRegions.yaml").write_text(
        """
- regionID: 10000033
  regionName: The Citadel
""".strip(),
        encoding="utf-8",
    )
    (bsd_dir / "mapConstellations.yaml").write_text(
        """
- constellationID: 20000345
  regionID: 10000033
""".strip(),
        encoding="utf-8",
    )
    (bsd_dir / "mapSolarSystems.yaml").write_text(
        """
- solarSystemID: 30002813
  solarSystemName: Tama
  constellationID: 20000345
  regionID: 10000033
  security: 0.3
  x: -10.0
  z: 50.0
- solarSystemID: 30002819
  solarSystemName: Kedama
  constellationID: 20000345
  regionID: 10000033
  security: 0.2
  x: 90.0
  z: -20.0
""".strip(),
        encoding="utf-8",
    )
    (bsd_dir / "mapSolarSystemJumps.yaml").write_text(
        """
- fromSolarSystemID: 30002813
  toSolarSystemID: 30002819
""".strip(),
        encoding="utf-8",
    )


def test_health_and_cors_preflight(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/health")
        assert status == 200
        assert payload["health"]["ok"] is True
        assert payload["health"]["schema_version"] == "health.v1"
        assert payload["health"]["storage"]["type"] == "IntelStore"
        assert payload["health"]["storage"]["path"].endswith("intel.json")
        assert payload["health"]["storage"]["writable"] is True
        assert payload["health"]["config"] == {"enabled": False}
        assert payload["health"]["esi"]["enabled"] is False
        assert payload["health"]["esi"]["authenticated"] is False
        assert payload["health"]["esi"]["config"] == {}
        assert payload["health"]["killboard"] == {"enabled": False}
        assert payload["health"]["events"]["alert_query_ok"] is True
        assert payload["health"]["events"]["sse"]["path"] == "/api/v1/events"
        assert payload["health"]["events"]["sse"]["legacy_path"] == "/api/events"

        request = Request(f"{server.url}/api/intel", method="OPTIONS")
        with urlopen(request, timeout=3) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Origin"] == "*"
            assert "DELETE" in response.headers["Access-Control-Allow-Methods"]
    finally:
        server.stop()


def test_health_does_not_generate_alerts(tmp_path):
    class CountingStore(IntelStore):
        def __init__(self, filepath):
            super().__init__(filepath)
            self.list_alerts_calls = 0

        def list_alerts(self, *args, **kwargs):
            self.list_alerts_calls += 1
            return super().list_alerts(*args, **kwargs)

    store = CountingStore(tmp_path / "intel.json")
    store.add_observation(
        {
            "source": "intel_channel",
            "system_name": "Tama",
            "names": ["Alice"],
            "seen_at": "2026-06-29T12:00:00+00:00",
        }
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/health")

        assert status == 200
        assert payload["health"]["events"]["alert_query_ok"] is True
        assert payload["health"]["events"]["sse"]["enabled"] is True
        assert "latest_alert_id" not in payload["health"]["events"]
        assert store.list_alerts_calls == 0
    finally:
        server.stop()


def test_health_reports_config_and_sqlite(tmp_path):
    config_store = IntelConfigStore(tmp_path / "intel_config.json")
    store = SQLiteIntelStore(
        tmp_path / "intel.sqlite3",
        scorer=config_store.build_scorer(),
        enricher=ThreatEnricher(),
    )
    store.add_observation(
        {
            "source": "manual",
            "source_instance": "health-test",
            "system_name": "Tama",
            "names": ["Known Hostile"],
            "raw_text": "Known Hostile in Tama",
        }
    )
    server = IntelHTTPServer(store, port=0, config_store=config_store)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/health")
        assert status == 200
        health = payload["health"]
        assert health["storage"]["type"] == "SQLiteIntelStore"
        assert health["storage"]["path"].endswith("intel.sqlite3")
        assert health["storage"]["writable"] is True
        assert health["config"]["enabled"] is True
        assert health["config"]["schema_version"] == "scoring_config.v1"
        assert health["config"]["scoring_version"] == CLASSIFICATION_VERSION
        assert health["config"]["evidence_rule_count"] > 0
        assert health["killboard"] == {"enabled": False}
        assert health["events"]["alert_query_ok"] is True
        assert health["events"]["sse"]["enabled"] is True
        assert "latest_alert_id" not in health["events"]
    finally:
        server.stop()


def test_health_reports_postgres_storage_without_secret(tmp_path):
    store = IntelStore(tmp_path / "intel.json")
    store._postgres_safe_dsn = "postgresql://***@db.internal:5432/eve_sentry"
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/health")
        assert status == 200
        storage = payload["health"]["storage"]
        assert storage["type"] == "IntelStore"
        assert storage["path"] == ""
        assert storage["dsn"] == "postgresql://***@db.internal:5432/eve_sentry"
        assert storage["writable"] is True
        assert "secret" not in json.dumps(storage)
    finally:
        server.stop()


def test_v1_bootstrap_and_map_routes_expose_workbench_payload(tmp_path):
    config_store = IntelConfigStore(tmp_path / "intel_config.json")
    config_store.update({"blacklist": ["Alice"]})
    server = IntelHTTPServer(
        IntelStore(
            tmp_path / "intel.json",
            systems={
                "Tama": StarSystem(name="Tama", system_id=30002813, x=10, y=20),
                "Kedama": StarSystem(name="Kedama", system_id=30002819, x=30, y=40),
            },
            links=[("Tama", "Kedama")],
            scorer=config_store.build_scorer(),
        ),
        port=0,
        config_store=config_store,
    )
    server.start()
    try:
        request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "raw_text": "Tama Alice",
                "seen_at": "2099-06-29T12:00:00+00:00",
            },
        )
        request_json(
            f"{server.url}/api/heartbeats",
            method="POST",
            payload={
                "client_id": "alert-client:test",
                "client_type": "alert_client",
                "label": "Alert Client",
                "heartbeat_interval_seconds": 5,
                "details": {"transport": "poll"},
            },
        )

        status, payload = request_json(f"{server.url}/api/v1/bootstrap")
        assert status == 200
        bootstrap = payload["bootstrap"]
        assert bootstrap["schema_version"] == "intel_bootstrap.v1"
        assert bootstrap["map"]["summary"]["system_count"] == 2
        assert bootstrap["map"]["systems"][0]["name"] in {"Kedama", "Tama"}
        assert bootstrap["reports"][0]["system_name"] == "Tama"
        assert bootstrap["observations"][0]["system_name"] == "Tama"
        assert bootstrap["alerts"][0]["system_name"] == "Tama"
        assert bootstrap["alerts"][0]["classification"] == "red"
        assert bootstrap["clients"]["summary"]["count"] == 1
        assert bootstrap["config"]["schema_version"] == "scoring_config.v1"
        assert bootstrap["esi"]["enabled"] is False
        assert bootstrap["esi"]["authenticated"] is False
        assert bootstrap["esi"]["config"] == {}

        status, map_payload = request_json(f"{server.url}/api/v1/map")
        assert status == 200
        assert map_payload["map"]["summary"]["system_count"] == 2
        assert map_payload["map"]["links"] == [{"from": "Tama", "to": "Kedama"}]
    finally:
        server.stop()


def test_v1_ocr_snapshot_endpoint_updates_active_intel(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, result = request_json(
            f"{server.url}/api/v1/ocr/snapshot",
            method="POST",
            payload={
                "client_id": "detector-client:test",
                "source_instance": "EVE - Hajimi6",
                "system_name": "S-KSWL",
                "seen_at": "2026-07-03T10:00:00+00:00",
                "names": ["Alice"],
            },
        )
        status2, active = request_json(f"{server.url}/api/v1/active-intel")

        assert status == 201
        assert result["created"] == 1
        assert result["active_count"] == 1
        assert "active" not in result
        assert status2 == 200
        assert active["count"] == 1
        assert active["active_intel"][0]["name"] == "Alice"
    finally:
        server.stop()


def test_v1_alerts_do_not_fabricate_alerts_from_ocr_active_intel(tmp_path):
    server = IntelHTTPServer(
        IntelStore(
            tmp_path / "intel.json",
            systems={},
            links=[],
            scorer=ScoringEngine(cooldown_seconds=0),
        ),
        port=0,
    )
    server.start()
    try:
        status, result = request_json(
            f"{server.url}/api/v1/ocr/snapshot",
            method="POST",
            payload={
                "client_id": "detector-client:test",
                "source_instance": "EVE - Hajimi6",
                "system_name": "S-KSWL",
                "seen_at": "2026-07-08T08:00:00+00:00",
                "names": ["Dictator 74"],
            },
        )
        status2, payload = request_json(f"{server.url}/api/v1/alerts")

        assert status == 201
        assert result["created"] == 1
        assert status2 == 200
        assert payload["alerts"] == []
    finally:
        server.stop()


def test_v1_bootstrap_includes_active_intel(tmp_path):
    store = IntelStore(tmp_path / "intel.json", systems={}, links=[])
    store.record_ocr_snapshot(
        {
            "client_id": "detector-client:test",
            "source_instance": "EVE - Hajimi6",
            "system_name": "S-KSWL",
            "seen_at": "2026-07-03T10:00:00+00:00",
            "names": ["Alice"],
        }
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/v1/bootstrap")

        assert status == 200
        assert payload["bootstrap"]["active_intel"][0]["name"] == "Alice"
    finally:
        server.stop()


def test_bootstrap_event_fingerprint_ignores_volatile_refresh_fields():
    handler = object.__new__(IntelRequestHandler)
    payload = {
        "schema_version": "intel_bootstrap.v1",
        "generated_at": "2026-07-23T01:00:00+00:00",
        "active_intel": [
            {
                "id": "ocr:alice",
                "name": "Alice",
                "last_seen_at": "2026-07-23T01:00:00+00:00",
                "metadata": {
                    "identity_checked_at": "2026-07-23T01:00:00+00:00",
                    "alliance_name": "Example Alliance",
                },
            }
        ],
        "clients": {
            "heartbeats": [
                {
                    "client_id": "detector-client:test",
                    "seen_at": "2026-07-23T01:00:00+00:00",
                    "age_seconds": 1.0,
                }
            ]
        },
    }
    refreshed = {
        **payload,
        "generated_at": "2026-07-23T01:00:05+00:00",
        "active_intel": [
            {
                **payload["active_intel"][0],
                "last_seen_at": "2026-07-23T01:00:05+00:00",
                "metadata": {
                    **payload["active_intel"][0]["metadata"],
                    "identity_checked_at": "2026-07-23T01:00:05+00:00",
                },
            }
        ],
        "clients": {
            "heartbeats": [
                {
                    "client_id": "detector-client:test",
                    "seen_at": "2026-07-23T01:00:05+00:00",
                    "age_seconds": 6.0,
                }
            ]
        },
    }

    assert handler._bootstrap_event_fingerprint(payload) == (
        handler._bootstrap_event_fingerprint(refreshed)
    )


def test_heartbeat_routes_and_health_summary(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/heartbeats",
            method="POST",
            payload={
                "client_id": "alert-client:test",
                "client_type": "alert_client",
                "label": "Alert Client",
                "heartbeat_interval_seconds": 5,
                "details": {"transport": "poll"},
            },
        )
        assert status == 201
        assert created["heartbeat"]["client_id"] == "alert-client:test"
        assert created["heartbeat"]["online"] is True
        assert created["heartbeat"]["details"]["transport"] == "poll"

        status, payload = request_json(f"{server.url}/api/heartbeats")
        assert status == 200
        assert payload["count"] == 1
        assert payload["heartbeats"][0]["client_type"] == "alert_client"
        assert payload["summary"]["count"] == 1
        assert payload["summary"]["online_count"] == 1
        assert payload["summary"]["stale_count"] == 0
        assert payload["summary"]["by_type"] == {"alert_client": 1}
        assert payload["summary"]["by_status"] == {"running": 1}

        status, payload = request_json(f"{server.url}/api/health")
        assert status == 200
        assert payload["health"]["clients"]["count"] == 1
        assert payload["health"]["clients"]["online_count"] == 1
    finally:
        server.stop()


def test_esi_status_reports_disabled_session(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/esi/status")
        assert status == 200
        assert payload["enabled"] is False
        assert payload["authenticated"] is False
        assert payload["config"] == {}

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


def test_esi_status_reports_public_resolver_without_session(tmp_path):
    class FakeResolver:
        def resolve_names(self, names):
            return []

    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json", resolver=FakeResolver()),
        port=0,
        esi_config={
            "client_id_configured": False,
            "token_file": str(tmp_path / "esi_tokens.json"),
            "token_file_present": False,
            "token_storage": "plain",
            "scopes": ["esi-location.read_location.v1"],
        },
    )
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/v1/esi/status")
        assert status == 200
        assert payload["enabled"] is True
        assert payload["public"] is True
        assert payload["authenticated"] is False
        assert payload["session"] is False
        assert payload["config"]["client_id_configured"] is False
        assert payload["config"]["token_file_present"] is False

        status, health = request_json(f"{server.url}/api/health")
        assert status == 200
        assert health["health"]["esi"]["enabled"] is True
        assert health["health"]["esi"]["public"] is True
        assert health["health"]["esi"]["authenticated"] is False
        assert health["health"]["esi"]["config"]["token_storage"] == "plain"
    finally:
        server.stop()


def test_v1_esi_login_route_reports_missing_configuration(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        try:
            request_json(f"{server.url}/api/v1/esi/login", method="POST")
        except HTTPError as exc:
            assert exc.code == 404
            error = json.loads(exc.read().decode("utf-8"))
            assert "ESI login" in error["error"]
        else:
            raise AssertionError("expected HTTP 404")
    finally:
        server.stop()


def test_v1_esi_login_route_starts_configured_flow(tmp_path):
    class FakeLogin:
        def __init__(self):
            self.calls = 0

        def start(self):
            self.calls += 1
            return {
                "status": "pending",
                "authorization_url": "https://login.test/authorize",
                "started_at": 1000,
                "expires_at": 1300,
                "timeout_seconds": 300,
                "character_id": None,
                "error": "",
            }

        def snapshot(self):
            return {
                "status": "pending",
                "authorization_url": "https://login.test/authorize",
                "started_at": 1000,
                "expires_at": 1300,
                "timeout_seconds": 300,
                "character_id": None,
                "error": "",
            }

    login = FakeLogin()
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json"),
        port=0,
        esi_login=login,
    )
    server.start()
    try:
        status, payload = request_json(
            f"{server.url}/api/v1/esi/login",
            method="POST",
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["login"]["status"] == "pending"
        assert payload["login"]["authorization_url"] == "https://login.test/authorize"
        assert login.calls == 1

        status, snapshot = request_json(f"{server.url}/api/v1/esi/login")
        assert status == 200
        assert snapshot["login"]["status"] == "pending"
        assert snapshot["login"]["authorization_url"] == "https://login.test/authorize"
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
    token_file = tmp_path / "esi_tokens.json"
    token_file.write_text("{}", encoding="utf-8")
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json", resolver=FakeResolver()),
        port=0,
        esi_session=session,
        esi_config={
            "client_id_configured": True,
            "token_file": str(token_file),
            "token_file_present": True,
            "token_storage": "plain",
            "scopes": ["esi-location.read_location.v1"],
        },
    )
    server.start()
    try:
        status, status_payload = request_json(f"{server.url}/api/esi/status")
        assert status == 200
        assert status_payload["enabled"] is True
        assert status_payload["authenticated"] is True
        assert status_payload["character_id"] == 123
        assert status_payload["config"]["client_id_configured"] is True
        assert status_payload["config"]["token_file_present"] is True
        assert "client_id" not in status_payload["config"]
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
        esi_config={
            "client_id_configured": True,
            "token_file": str(tmp_path / "missing_esi_tokens.json"),
            "token_file_present": False,
        },
    )
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/esi/status")
        assert status == 200
        assert payload["authenticated"] is False
        assert payload["config"]["token_file_present"] is False
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


def test_esi_status_refreshes_token_file_presence_after_start(tmp_path):
    class MissingTokenSession:
        def load_tokens(self, refresh_if_needed=True):
            raise EsiSsoError("no saved ESI token")

    token_file = tmp_path / "late_esi_tokens.json"
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json"),
        port=0,
        esi_session=MissingTokenSession(),
        esi_config={
            "client_id_configured": True,
            "token_file": str(token_file),
            "token_file_present": False,
        },
    )
    server.start()
    try:
        status, before = request_json(f"{server.url}/api/v1/esi/status")
        assert status == 200
        assert before["config"]["token_file_present"] is False

        token_file.write_text("{}", encoding="utf-8")

        status, after = request_json(f"{server.url}/api/v1/esi/status")
        assert status == 200
        assert after["config"]["token_file_present"] is True
    finally:
        server.stop()


def test_root_path_does_not_serve_legacy_html(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        request = Request(f"{server.url}/")
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            assert exc.code == 404
            assert exc.headers["Content-Type"].startswith("application/json")
            assert json.loads(body) == {"error": "not found"}
        else:
            raise AssertionError("expected root path to return 404")
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


def test_public_lookup_routes_return_profiles_without_killboard_activity(tmp_path):
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

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel.json",
        resolver=resolver,
        enricher=ThreatEnricher(resolver=resolver),
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

        status, by_name = request_json(
            f"{server.url}/api/v1/characters/by-name/Alice"
        )
        assert status == 200
        assert by_name["character"]["character_id"] == 123
        assert by_name["character"]["corporation_id"] == 456

        status, by_id = request_json(f"{server.url}/api/v1/characters/123")
        assert status == 200
        assert by_id["character"]["alliance_id"] == 789

        status, system = request_json(f"{server.url}/api/v1/systems/by-name/Tama")
        assert status == 200
        assert system["system"]["system_id"] == 30002813
        assert system["system"]["security_status"] == 0.3

        status, system = request_json(f"{server.url}/api/v1/systems/30002813")
        assert status == 200
        assert system["system"]["name"] == "Tama"
        assert system["system"]["system_id"] == 30002813

    finally:
        server.stop()


def test_v1_map_system_route_returns_profile_and_intel(tmp_path):
    class FakeResolver:
        def system_profile(self, system_id):
            assert system_id == 30002813
            return {
                "system_id": 30002813,
                "name": "Tama",
                "security_status": 0.3,
            }

    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json", resolver=FakeResolver()),
        port=0,
    )
    server.start()
    try:
        request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "system_id": 30002813,
                "names": ["Alice"],
                "source": "intel_channel",
                "raw_text": "Tama Alice",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )

        status, payload = request_json(f"{server.url}/api/v1/map/systems/30002813")
        assert status == 200
        assert payload["system"]["profile"]["name"] == "Tama"
        assert payload["system"]["intel"]["entity"]["type"] == "system"
        assert payload["system"]["intel"]["alerts"][0]["system_id"] == 30002813
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
            request_json(f"{server.url}/api/v1/characters/123")
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
            request_json(f"{server.url}/api/v1/kill-activity/character/123")
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
            request_json(f"{server.url}/api/v1/kill-activity/corporation/456")
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
            request_json(
                f"{server.url}/api/v1/kill-activity/character/not-a-number"
            )
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

        try:
            request_json(f"{server.url}/api/v1/kill-activity/alliance/not-a-number")
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
                "source": "intel_channel",
                "system_name": "Tama",
                "names": ["Alice"],
                "character_ids": [123],
                "metadata": {"hostile_count": 1},
                "seen_at": "2026-06-30T12:00:00+00:00",
            },
        )

        assert status == 201
        evidence_types = {item["type"] for item in created["alert"]["evidence"]}
        assert "hostile_standing" in evidence_types
        assert created["alert"]["score"] == 100

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

    resolver = FakeResolver()
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_alliance_ids={789}),
            cooldown_seconds=0,
        ),
        enricher=ThreatEnricher(resolver=resolver),
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
        assert detail["schema_version"] == "alert_detail.v1"
        assert detail["alert"]["id"] == created["alert"]["id"]
        assert detail["observation"]["id"] == created["observation"]["id"]
        assert detail["entities"]["characters"][0]["character_id"] == 123
        assert detail["entities"]["characters"][0]["name"] == "Alice"
        assert detail["entities"]["systems"] == [
            {"system_id": None, "name": "Tama"}
        ]
        assert detail["entities"]["corporations"] == [{"corporation_id": 456}]
        assert detail["entities"]["alliances"] == [{"alliance_id": 789}]
        assert detail["context"]["resolution"] == {}
        assert detail["context"]["channel_mentions"][0]["relation"] == "same_system"
        assert detail["context"]["character_profiles"][0]["character_id"] == 123
        assert detail["context"]["kill_activities"] == []
        assert detail["context"]["group_activities"] == []
        assert detail["explanation"]["summary"].startswith(
            "CRITICAL alert for Alice in Tama"
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
        assert detail["explanation"]["degraded_sources"] == []

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


def test_alert_detail_route_reports_degraded_sources_without_enrichment(tmp_path):
    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "source": "intel_channel",
                "system_name": "Tama",
                "names": ["Alice"],
                "character_ids": [123],
                "metadata": {"hostile_count": 1},
                "seen_at": "2026-06-30T12:00:00+00:00",
            },
        )
        assert status == 201

        status, payload = request_json(
            f"{server.url}/api/alerts/{created['alert']['id']}"
        )

        assert status == 200
        detail = payload["detail"]
        assert detail["schema_version"] == "alert_detail.v1"
        assert detail["entities"]["characters"] == [{"character_id": 123}]
        assert detail["context"]["character_profiles"] == []
        assert detail["context"]["kill_activities"] == []
        assert detail["explanation"]["degraded_sources"] == [
            {
                "source": "esi",
                "reason": "character profiles unavailable",
            },
        ]
    finally:
        server.stop()


def test_alert_detail_route_includes_esi_cache_status_in_explanation(tmp_path):
    class FakeResolver:
        def character_profile(self, character_id):
            assert character_id == 123
            return {
                "character_id": 123,
                "name": "Alice",
                "corporation_id": 456,
                "cache_status": "cached",
            }

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_alliance_ids={789}),
            cooldown_seconds=0,
        ),
        enricher=ThreatEnricher(resolver=FakeResolver()),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "source": "intel_channel",
                "system_name": "Tama",
                "names": ["Alice"],
                "character_ids": [123],
                "metadata": {"hostile_count": 1},
                "seen_at": "2026-06-30T12:00:00+00:00",
            },
        )
        assert status == 201

        status, payload = request_json(
            f"{server.url}/api/alerts/{created['alert']['id']}"
        )

        assert status == 200
        assert payload["detail"]["context"]["character_profiles"][0][
            "cache_status"
        ] == "cached"
        assert "ESI profile Alice: corp 456, cache cached" in (
            payload["detail"]["explanation"]["context"]
        )
    finally:
        server.stop()


def test_entity_intel_routes_return_related_alerts_and_enrichment(tmp_path):
    class FakeResolver:
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
            return {"system_id": 30002813, "name": "Tama"}

    store = IntelStore(
        tmp_path / "intel.json",
        systems={},
        links=[],
        scorer=ScoringEngine(
            watchlist=Watchlist(hostile_alliance_ids={789}),
            cooldown_seconds=0,
        ),
        enricher=ThreatEnricher(resolver=FakeResolver()),
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
                "system_id": 30002813,
                "names": ["Alice"],
                "character_ids": [123],
                "seen_at": "2026-06-30T12:00:00+00:00",
            },
        )
        assert status == 201

        for path, entity_type, entity_id in [
            ("/api/intel/character/123", "character", 123),
            ("/api/intel/system/30002813", "system", 30002813),
            ("/api/intel/corporation/456", "corporation", 456),
            ("/api/intel/alliance/789", "alliance", 789),
        ]:
            status, payload = request_json(f"{server.url}{path}")
            assert status == 200
            intel = payload["intel"]
            assert intel["schema_version"] == "intel_entity.v1"
            assert intel["entity"]["type"] == entity_type
            assert intel["entity"]["id"] == entity_id
            assert intel["observations"][0]["id"] == created["observation"]["id"]
            assert intel["alerts"][0]["id"] == created["alert"]["id"]
            assert intel["counts"]["observations"] == 1
            assert intel["counts"]["alerts"] == 1
            assert intel["counts"]["has_activity"] is False

        status, payload = request_json(
            f"{server.url}/api/intel/character/123?min_level=critical"
        )
        assert status == 200
        assert payload["intel"]["counts"]["observations"] == 1
        assert payload["intel"]["counts"]["alerts"] == 1

        try:
            request_json(f"{server.url}/api/intel/character/not-an-id")
        except HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("expected HTTP 400")
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
        assert first["parsed"]["metadata"]["parse_diagnostics"] == {
            "parse_pattern": "leading_system",
            "system_candidates": ["Tama"],
            "ignored_tokens": ["+3", "reds"],
        }
        assert first["observation"]["source_instance"] == "Alliance Intel"
        assert first["observation"]["metadata"]["sender"] == "Scout A"
        assert first["observation"]["metadata"]["parse_diagnostics"] == {
            "parse_pattern": "leading_system",
            "system_candidates": ["Tama"],
            "ignored_tokens": ["+3", "reds"],
        }
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


def test_create_channel_line_does_not_treat_repeated_sender_as_target(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": "[ 2026.06.30 12:01:12 ] Scout A > Scout A Tama +3 reds",
            },
        )

        assert status == 201
        assert created["parsed"]["system_name"] == "Tama"
        assert created["parsed"]["names"] == []
        assert created["observation"]["names"] == []
        assert created["observation"]["metadata"]["sender"] == "Scout A"
        assert created["observation"]["raw_text"] == "Scout A: Tama +3 reds"
    finally:
        server.stop()


def test_create_channel_line_does_not_treat_inline_sender_as_target(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": (
                    "[ 2026.06.30 12:01:12 ] Scout A > "
                    "stoneyflap: 8-4GQM Hector Audeles"
                ),
            },
        )

        assert status == 201
        assert created["parsed"]["system_name"] == "8-4GQM"
        assert created["parsed"]["names"] == ["Hector Audeles"]
        assert created["observation"]["names"] == ["Hector Audeles"]
        assert "stoneyflap" not in created["observation"]["names"]
    finally:
        server.stop()


def test_create_channel_line_keeps_low_confidence_raw_observation_without_alert(
    tmp_path,
):
    store = IntelStore(
        tmp_path / "intel.json",
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": "[ 2026.06.30 12:01:12 ] Scout A > no useful structure here",
            },
        )

        assert status == 201
        assert created["ignored"] is False
        assert created["parsed"]["system_name"] == "Unknown"
        assert created["parsed"]["confidence"] == 0.2
        assert created["parsed"]["metadata"]["parse_diagnostics"] == {
            "parse_pattern": "raw_unparsed",
            "system_candidates": ["useful", "structure", "here"],
            "name_candidates": ["no useful structure here"],
        }
        assert created["alert"] is None

        status, observations = request_json(f"{server.url}/api/observations")
        assert status == 200
        assert observations["count"] == 1
        assert observations["observations"][0]["raw_text"] == (
            "Scout A: no useful structure here"
        )

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 0
    finally:
        server.stop()


def test_create_channel_line_repairs_unique_esi_system_match(tmp_path):
    class FakeResolver:
        def resolve_names(self, names):
            assert names == ["Alice", "Tama"]
            return [
                SimpleNamespace(
                    name="Alice",
                    category="character",
                    entity_id=123,
                ),
                SimpleNamespace(
                    name="Tama",
                    category="solar_system",
                    entity_id=30002813,
                ),
            ]

        def enrich_observation(self, observation):
            observation.system_id = 30002813
            observation.character_ids = [123]
            return observation

    store = IntelStore(
        tmp_path / "intel.json",
        resolver=FakeResolver(),
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": "[ 2026.06.30 12:01:12 ] Scout A > Alice reds Tama",
            },
        )

        assert status == 201
        assert created["ignored"] is False
        assert created["observation"]["system_name"] == "Tama"
        assert created["observation"]["system_id"] == 30002813
        assert created["observation"]["names"] == ["Alice"]
        assert created["observation"]["character_ids"] == [123]
        assert created["observation"]["metadata"]["esi_resolution"] == {
            "candidate_system_names": ["Alice", "Tama"],
            "resolved_system_candidates": ["Tama"],
            "system_repair_status": "repaired",
            "system_repaired_from": "Alice",
            "system_repaired_to": "Tama",
        }
        assert created["alert"]["score"] == 30

        status, observations = request_json(f"{server.url}/api/observations")
        assert status == 200
        assert observations["observations"][0]["system_name"] == "Tama"

        status, detail_payload = request_json(
            f"{server.url}/api/alerts/{created['alert']['id']}"
        )
        assert status == 200
        assert (
            "ESI repaired channel system Alice -> Tama from candidates Alice, Tama"
            in detail_payload["detail"]["explanation"]["context"]
        )
    finally:
        server.stop()


def test_create_channel_line_can_defer_expensive_enrichment(tmp_path):
    class ExplodingResolver:
        def resolve_names(self, names):
            raise AssertionError(f"resolve_names should be deferred: {names}")

        def enrich_observation(self, observation):
            raise AssertionError("enrich_observation should be deferred")

    store = IntelStore(
        tmp_path / "intel.json",
        resolver=ExplodingResolver(),
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds",
                "defer_enrichment": True,
            },
        )

        assert status == 201
        assert created["ignored"] is False
        assert created["observation"]["system_name"] == "Tama"
        assert created["observation"]["metadata"]["enrichment_deferred"] is True
        assert created["observation"]["metadata"]["raw_line"] == (
            "[ 2026.06.30 12:01:12 ] Scout A > Tama +3 reds"
        )
        assert created["alert"] is None
    finally:
        server.stop()


def test_create_channel_line_suppresses_invalid_system_after_esi_resolution(tmp_path):
    class FakeResolver:
        def enrich_observation(self, observation):
            metadata = dict(observation.metadata)
            metadata["esi_resolution"] = {
                "attempted": True,
                "character_name_count": len(observation.names),
                "resolved_character_count": len(observation.character_ids),
                "system_name_matched": False,
            }
            observation.metadata = metadata
            return observation

    store = IntelStore(
        tmp_path / "intel.json",
        resolver=FakeResolver(),
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": "[ 2026.06.30 12:01:12 ] Scout A > Alice reds",
            },
        )

        assert status == 201
        assert created["ignored"] is False
        assert created["parsed"]["system_name"] == "Alice"
        assert created["alert"] is None
        assert created["observation"]["metadata"]["esi_resolution"] == {
            "attempted": True,
            "character_name_count": 0,
            "resolved_character_count": 0,
            "system_name_matched": False,
        }

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 0
    finally:
        server.stop()


def test_create_channel_line_keeps_ambiguous_system_candidates_in_metadata(tmp_path):
    class AmbiguousRepairClient:
        def resolve_ids(self, names):
            if names == ["Alice", "Tama", "Oijanen"]:
                return {
                    "characters": [{"id": 123, "name": "Alice"}],
                    "systems": [
                        {"id": 30002813, "name": "Tama"},
                        {"id": 30002814, "name": "Oijanen"},
                    ],
                }
            if names == ["Alice"]:
                return {
                    "characters": [{"id": 123, "name": "Alice"}],
                }
            if names == ["Tama Oijanen"]:
                return {
                    "characters": [{"id": 123, "name": "Alice"}],
                }
            raise AssertionError(names)

    store = IntelStore(
        tmp_path / "intel.json",
        resolver=EsiResolver(
            client=AmbiguousRepairClient(),
            cache=EsiCache(tmp_path / "esi.json"),
        ),
        scorer=ScoringEngine(cooldown_seconds=0),
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, created = request_json(
            f"{server.url}/api/channel-lines",
            method="POST",
            payload={
                "channel": "Alliance Intel",
                "line": (
                    "[ 2026.06.30 12:01:12 ] Scout A > "
                    "Alice reds Tama Oijanen"
                ),
            },
        )

        assert status == 201
        assert created["ignored"] is False
        assert created["observation"]["system_name"] == "Alice"
        assert created["observation"]["names"] == []
        assert created["alert"] is None
        assert created["observation"]["metadata"]["esi_resolution"] == {
            "attempted": True,
            "candidate_system_names": ["Alice", "Tama", "Oijanen"],
            "character_name_count": 0,
            "resolved_character_count": 0,
            "resolved_system_candidates": ["Tama", "Oijanen"],
            "system_name_matched": False,
            "system_repair_status": "ambiguous",
            "suppressed_name_candidates": ["Tama Oijanen"],
        }

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 0
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
        events = sse_events(body)
        payload = next(item["data"] for item in events if item.get("event") == "alert")
        assert payload["id"] == created["alert"]["id"]
    finally:
        server.stop()


def test_v1_events_stream_returns_alert_sse(tmp_path):
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
            },
        )
        assert status == 201

        status, headers, body = request_text(
            f"{server.url}/api/v1/events?{urlencode({'timeout': '0', 'limit': '5'})}"
        )

        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "event: bootstrap" not in body
        assert "event: alert" in body

        status, headers, body = request_text(
            f"{server.url}/api/v1/events?"
            f"{urlencode({'timeout': '0', 'limit': '5', 'bootstrap': '1'})}"
        )

        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "event: bootstrap" in body
        assert "event: alert" in body
        events = sse_events(body)
        bootstrap = next(
            item["data"] for item in events if item.get("event") == "bootstrap"
        )
        payload = next(item["data"] for item in events if item.get("event") == "alert")
        assert bootstrap["schema_version"] == "intel_bootstrap.v1"
        assert bootstrap["map"]["summary"]["alert_count"] == 1
        assert payload["id"] == created["alert"]["id"]

        status, headers, body = request_text(
            f"{server.url}/api/v1/events?"
            f"{urlencode({'timeout': '0', 'limit': '5', 'bootstrap': '0'})}"
        )
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "event: bootstrap" not in body
        assert "event: alert" in body
    finally:
        server.stop()


def test_v1_active_alerts_exclude_friendly_classifications(tmp_path):
    class FriendlyScorer:
        def score(self, observation, **kwargs):
            return ThreatEvent(
                event_id=f"evt_{observation.observation_id}",
                system_name=observation.system_name,
                system_id=observation.system_id,
                names=list(observation.names),
                character_ids=list(observation.character_ids),
                score=1,
                level="low",
                evidence=[
                    Evidence(
                        "friendly_standing",
                        1,
                        "Friendly standing 10",
                    )
                ],
                source_observation_id=observation.observation_id,
                created_at=observation.received_at,
                scoring_version="classification.v1",
                classification="white",
                reason="Friendly standing 10",
            )

    store = IntelStore(tmp_path / "intel.json", scorer=FriendlyScorer())
    store.add_observation(
        {
            "system_name": "Tama",
            "names": ["Friendly Pilot"],
            "source": "intel_channel",
            "seen_at": "2026-07-10T01:46:36+00:00",
        }
    )
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, bootstrap_payload = request_json(f"{server.url}/api/v1/bootstrap")
        assert status == 200
        bootstrap = bootstrap_payload["bootstrap"]
        assert bootstrap["alerts"] == []
        assert bootstrap["map"]["summary"]["alert_count"] == 0

        status, alerts = request_json(f"{server.url}/api/v1/alerts")
        assert status == 200
        assert alerts == {"alerts": [], "count": 0}

        status, headers, body = request_text(
            f"{server.url}/api/v1/events?{urlencode({'timeout': '0', 'limit': '5'})}"
        )
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "event: alert" not in body
    finally:
        server.stop()


def test_v1_alerts_ignores_legacy_active_item_without_source_ids(tmp_path):
    store = IntelStore(tmp_path / "intel.json")
    observation = store.add_observation(
        {
            "system_name": "Tama",
            "names": ["Alice"],
            "source": "intel_channel",
        }
    )
    active = store.list_active_intel()[0]
    active["source_observation_ids"] = None

    class LegacyActiveStore(IntelStore):
        def list_active_intel(self, *args, **kwargs):
            return [active]

    server = IntelHTTPServer(LegacyActiveStore(tmp_path / "legacy.json"), port=0)
    server.store._reports = store._reports
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/v1/alerts")
        assert status == 200
        assert payload["count"] == 0
        assert payload["alerts"] == []
    finally:
        server.stop()


def test_events_stream_sends_keepalive_comments_when_idle(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        status, headers, body = request_text(
            f"{server.url}/api/events?"
            f"{urlencode({'timeout': '0.05', 'heartbeat': '0.01'})}"
        )

        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert ": keepalive" in body
        assert "event: alert" not in body
    finally:
        server.stop()


def test_events_stream_supports_multiple_concurrent_subscribers(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        url = (
            f"{server.url}/api/events?"
            f"{urlencode({'timeout': '0.2', 'heartbeat': '0', 'limit': '5'})}"
        )
        results: dict[str, tuple[int, str] | Exception] = {}
        errors: list[Exception] = []

        def consume(name: str) -> None:
            try:
                status, _, body = request_text(url, timeout=5)
                results[name] = (status, body)
            except Exception as exc:  # pragma: no cover - surfaced by assertions
                results[name] = exc
                errors.append(exc)

        first = threading.Thread(target=consume, args=("first",), daemon=True)
        second = threading.Thread(target=consume, args=("second",), daemon=True)
        first.start()
        second.start()

        time.sleep(0.05)
        _, created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
            },
        )

        first.join(timeout=6)
        second.join(timeout=6)

        assert not errors
        assert "first" in results and "second" in results
        first_status, first_body = results["first"]
        second_status, second_body = results["second"]
        assert first_status == 200
        assert second_status == 200
        assert created["alert"]["id"] in first_body
        assert created["alert"]["id"] in second_body
    finally:
        server.stop()


def test_events_stream_resumes_from_last_event_id(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        _, first_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
                "received_at": "2026-06-29T12:30:00+00:00",
            },
        )
        _, second_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Bob"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:01:00+00:00",
                "received_at": "2026-06-29T12:30:00+00:00",
            },
        )

        status, _, body = request_text(
            f"{server.url}/api/events?{urlencode({'timeout': '0', 'limit': '5'})}",
            headers={"Last-Event-ID": first_created["alert"]["id"]},
        )

        assert status == 200
        assert first_created["alert"]["id"] not in body
        assert second_created["alert"]["id"] in body
    finally:
        server.stop()


def test_events_stream_prefers_last_event_id_over_stale_since(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        _, first_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
            },
        )
        _, second_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Bob"],
                "source": "intel_channel",
            },
        )

        query = urlencode(
            {
                "timeout": "0",
                "limit": "5",
                "since": "2020-01-01T00:00:00+00:00",
            }
        )
        status, _, body = request_text(
            f"{server.url}/api/events?{query}",
            headers={"Last-Event-ID": first_created["alert"]["id"]},
        )

        assert status == 200
        assert first_created["alert"]["id"] not in body
        assert second_created["alert"]["id"] in body
    finally:
        server.stop()


def test_v1_events_bootstrap_id_is_resumable_alert_cursor(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        _, first_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
            },
        )

        query = urlencode({"timeout": "0", "limit": "5", "bootstrap": "1"})
        status, _, body = request_text(f"{server.url}/api/v1/events?{query}")
        assert status == 200
        first_events = sse_events(body)
        bootstrap_event = next(
            event for event in first_events if event.get("event") == "bootstrap"
        )
        assert bootstrap_event["id"] == first_created["alert"]["id"]

        resume_query = urlencode(
            {
                "timeout": "0",
                "limit": "5",
                "since": "2020-01-01T00:00:00+00:00",
            }
        )
        status, _, resumed_body = request_text(
            f"{server.url}/api/v1/events?{resume_query}",
            headers={"Last-Event-ID": bootstrap_event["id"]},
        )
        assert status == 200
        assert [
            event for event in sse_events(resumed_body) if event.get("event") == "alert"
        ] == []
        assert first_created["alert"]["id"] not in {
            event.get("id")
            for event in sse_events(resumed_body)
            if event.get("event") == "alert"
        }
    finally:
        server.stop()


def test_events_stream_since_parameter_remains_exclusive(tmp_path):
    server = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    server.start()
    try:
        _, first_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
                "received_at": "2026-06-29T12:00:00+00:00",
            },
        )
        _, second_created = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Bob"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:01:00+00:00",
                "received_at": "2026-06-29T12:01:00+00:00",
            },
        )

        query = urlencode(
            {
                "timeout": "0",
                "limit": "5",
                "since": first_created["alert"]["created_at"],
            }
        )
        status, _, body = request_text(f"{server.url}/api/events?{query}")

        assert status == 200
        assert first_created["alert"]["id"] not in body
        assert second_created["alert"]["id"] in body
    finally:
        server.stop()


def test_sqlite_server_restart_preserves_ack_and_event_resume(tmp_path):
    db_path = tmp_path / "intel.sqlite3"
    first_server = IntelHTTPServer(
        SQLiteIntelStore(db_path, systems={}, links=[]),
        port=0,
    )
    first_server.start()
    try:
        _, first_created = request_json(
            f"{first_server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:00:00+00:00",
                "received_at": "2026-06-29T12:30:00+00:00",
            },
        )
        first_alert_id = first_created["alert"]["id"]

        status, acked = request_json(
            f"{first_server.url}/api/alerts/{first_alert_id}/ack",
            method="POST",
            payload={"acknowledged_by": "alert-client", "note": "sent"},
        )

        assert status == 200
        assert acked["alert"]["acknowledged"] is True
    finally:
        first_server.stop()

    second_server = IntelHTTPServer(
        SQLiteIntelStore(db_path, systems={}, links=[]),
        port=0,
    )
    second_server.start()
    try:
        status, acknowledged = request_json(
            f"{second_server.url}/api/alerts?{urlencode({'acknowledged': 'true'})}"
        )
        assert status == 200
        assert [alert["id"] for alert in acknowledged["alerts"]] == [first_alert_id]
        assert acknowledged["alerts"][0]["acknowledged_by"] == "alert-client"

        _, second_created = request_json(
            f"{second_server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Bob"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:01:00+00:00",
                "received_at": first_created["alert"]["created_at"],
            },
        )
        second_alert_id = second_created["alert"]["id"]

        status, _, body = request_text(
            f"{second_server.url}/api/events?"
            f"{urlencode({'timeout': '0', 'limit': '5'})}",
            headers={"Last-Event-ID": first_alert_id},
        )

        assert status == 200
        assert first_alert_id not in body
        assert second_alert_id in body
    finally:
        second_server.stop()


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
        assert created["alert"] is None

        status, updated = request_json(
            f"{server.url}/api/config",
            method="PUT",
            payload={"blacklist": ["Alice"], "cooldown_seconds": 0},
        )
        assert status == 200
        assert updated["config"]["blacklist"] == ["Alice"]
        assert updated["config"]["schema_version"] == "scoring_config.v1"
        assert updated["config"]["scoring_version"] == CLASSIFICATION_VERSION
        assert any(
            item["type"] == "blacklist_match"
            for item in updated["config"]["evidence_rules"]
        )

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 1
        assert alerts["alerts"][0]["classification"] == "red"

        status, classified = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Tama",
                "names": ["Alice"],
                "source": "intel_channel",
                "seen_at": "2026-06-29T12:01:00+00:00",
            },
        )
        assert status == 201
        assert classified["alert"]["classification"] == "red"
        assert classified["alert"]["scoring_version"] == CLASSIFICATION_VERSION

        status, observations = request_json(f"{server.url}/api/observations")
        assert status == 200
        assert observations["count"] == 2

        status, v1_observations = request_json(f"{server.url}/api/v1/observations")
        assert status == 200
        assert v1_observations["count"] == 2

        status, bootstrap_payload = request_json(f"{server.url}/api/v1/bootstrap")
        assert status == 200
        bootstrap = bootstrap_payload["bootstrap"]
        assert len(bootstrap["reports"]) == 2
        assert len(bootstrap["observations"]) == 2
        assert bootstrap["alerts"] == []

        status, config = request_json(f"{server.url}/api/config")
        assert status == 200
        assert config["config"]["blacklist"] == ["Alice"]
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


def test_map_config_api_updates_active_topology(tmp_path):
    map_config_store = MapConfigStore(tmp_path / "intel_map.json")
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json"),
        port=0,
        map_config_store=map_config_store,
    )
    server.start()
    try:
        status, updated = request_json(
            f"{server.url}/api/map/config",
            method="PUT",
            payload={
                "source": "manual",
                "layout_mode": "manual",
                "systems": [
                    {
                        "system_id": 30002813,
                        "name": "Tama",
                        "x": 100,
                        "y": 120,
                        "region": "The Citadel",
                        "security": 0.3,
                    },
                    {
                        "system_id": 30002819,
                        "name": "Kedama",
                        "x": 180,
                        "y": 180,
                        "region": "The Citadel",
                        "security": 0.2,
                    },
                ],
                "links": [{"from": "Tama", "to": "Kedama"}],
            },
        )
        assert status == 200
        assert updated["map"]["source"] == "manual"
        assert updated["counts"] == {"systems": 2, "links": 1}

        status, payload = request_json(f"{server.url}/api/systems")
        assert status == 200
        assert {item["name"] for item in payload["systems"]} == {"Tama", "Kedama"}
        assert payload["links"] == [{"from": "Tama", "to": "Kedama"}]

        status, report = request_json(
            f"{server.url}/api/observations",
            method="POST",
            payload={
                "system_name": "Jita",
                "names": ["Alice"],
                "source": "intel_channel",
                "raw_text": "Jita Alice",
                "seen_at": "2099-06-29T12:00:00+00:00",
            },
        )
        assert status == 201
        assert report["observation"]["system_name"] == "Jita"

        status, payload = request_json(f"{server.url}/api/systems")
        assert status == 200
        assert {item["name"] for item in payload["systems"]} == {"Tama", "Kedama"}

        status, health = request_json(f"{server.url}/api/health")
        assert status == 200
        assert health["health"]["map"]["enabled"] is True
        assert health["health"]["map"]["source"] == "manual"
        assert health["health"]["map"]["system_count"] == 2
    finally:
        server.stop()


def test_v1_map_filters_nodes_to_configured_topology(tmp_path):
    map_config_store = MapConfigStore(tmp_path / "intel_map.json")
    map_config_store.update(
        {
            "source": "manual",
            "layout_mode": "manual",
            "systems": [
                {
                    "system_id": 30002813,
                    "name": "Tama",
                    "x": 100,
                    "y": 120,
                    "region": "The Citadel",
                    "security": 0.3,
                },
                {
                    "system_id": 30002819,
                    "name": "Kedama",
                    "x": 180,
                    "y": 180,
                    "region": "The Citadel",
                    "security": 0.2,
                },
            ],
            "links": [{"from": "Tama", "to": "Kedama"}],
        }
    )
    store = IntelStore(
        tmp_path / "intel.json",
        systems={
            "Tama": StarSystem("Tama", 100, 120, "The Citadel", 0.3, 30002813),
            "Kedama": StarSystem(
                "Kedama",
                180,
                180,
                "The Citadel",
                0.2,
                30002819,
            ),
            "Jita": StarSystem("Jita", 240, 180, "The Forge", 0.9, 30000142),
        },
        links=[("Tama", "Kedama"), ("Kedama", "Jita")],
    )
    server = IntelHTTPServer(
        store,
        port=0,
        map_config_store=map_config_store,
    )
    server.start()
    try:
        status, payload = request_json(f"{server.url}/api/v1/map")
        assert status == 200
        assert {item["name"] for item in payload["map"]["systems"]} == {
            "Tama",
            "Kedama",
        }
        assert payload["map"]["links"] == [{"from": "Tama", "to": "Kedama"}]

        status, payload = request_json(f"{server.url}/api/v1/bootstrap")
        assert status == 200
        assert {item["name"] for item in payload["bootstrap"]["map"]["systems"]} == {
            "Tama",
            "Kedama",
        }
    finally:
        server.stop()


def test_map_refresh_api_imports_sde_topology(tmp_path):
    sde_root = tmp_path / "sde"
    write_sde_fixture(sde_root)
    map_config_store = MapConfigStore(tmp_path / "intel_map.json")
    server = IntelHTTPServer(
        IntelStore(tmp_path / "intel.json"),
        port=0,
        map_config_store=map_config_store,
    )
    server.start()
    try:
        status, refreshed = request_json(
            f"{server.url}/api/map/refresh",
            method="POST",
            payload={
                "source": "sde",
                "sde_path": str(sde_root),
                "region_ids": [10000033],
            },
        )
        assert status == 200
        assert refreshed["map"]["source"] == "sde"
        assert refreshed["counts"] == {"systems": 2, "links": 1}

        status, systems = request_json(f"{server.url}/api/systems")
        assert status == 200
        assert {item["name"] for item in systems["systems"]} == {"Tama", "Kedama"}
        assert systems["links"] == [{"from": "Tama", "to": "Kedama"}]

        status, config = request_json(f"{server.url}/api/map/config")
        assert status == 200
        assert config["map"]["sde_path"] == str(sde_root)
        assert config["map"]["last_refreshed_at"]
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
