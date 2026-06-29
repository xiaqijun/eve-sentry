import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
