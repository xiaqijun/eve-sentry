import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.intel.config import IntelConfigStore
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

        status, alerts = request_json(f"{server.url}/api/alerts")
        assert status == 200
        assert alerts["count"] == 1
        assert alerts["alerts"][0]["score"] == 30
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
