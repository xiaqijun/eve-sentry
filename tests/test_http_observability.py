"""Integration tests for HTTP request observability."""

import http.client
import json
import logging
import re
import time
from urllib.parse import urlparse

import pytest

from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@pytest.fixture
def server(tmp_path):
    store = IntelStore(tmp_path / "intel.json")
    instance = IntelHTTPServer(store, port=0)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()
        store.close()


def _request(server, path: str, headers: dict[str, str] | None = None):
    parsed = urlparse(server.url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, response.headers, json.loads(body.decode("utf-8"))
    finally:
        connection.close()


def _health(server) -> dict:
    status, _, payload = _request(server, "/api/health")
    assert status == 200
    return payload["health"]


@pytest.mark.parametrize(
    ("provided_request_id", "expected_request_id"),
    [
        (None, None),
        ("edge-01.trace_2", "edge-01.trace_2"),
        ("request id with spaces", None),
        ("a" * 65, None),
    ],
)
def test_response_request_id_is_echoed_or_safely_generated(
    server,
    provided_request_id,
    expected_request_id,
) -> None:
    headers = (
        {"X-Request-ID": provided_request_id}
        if provided_request_id is not None
        else None
    )

    status, response_headers, _ = _request(server, "/api/livez", headers)

    assert status == 200
    response_request_id = response_headers.get("X-Request-ID")
    exposed_headers = {
        name.strip().lower()
        for name in response_headers.get("Access-Control-Expose-Headers", "").split(",")
    }
    assert "x-request-id" in exposed_headers
    if expected_request_id is not None:
        assert response_request_id == expected_request_id
    else:
        assert response_request_id is not None
        assert REQUEST_ID_PATTERN.fullmatch(response_request_id)
        assert response_request_id != provided_request_id


def test_access_log_is_structured_and_excludes_query_secrets(server, caplog) -> None:
    request_id = "integration-log-request"
    secret = "do-not-log-this-token"
    caplog.set_level(logging.INFO, logger="app.server.http_server")
    caplog.clear()

    status, _, _ = _request(
        server,
        f"/api/health?token={secret}&redirect=/private",
        {"X-Request-ID": request_id},
    )

    deadline = time.monotonic() + 1
    access_records = []
    while time.monotonic() < deadline:
        access_records = [
            json.loads(record.getMessage())
            for record in caplog.records
            if record.name.startswith("app.server.http_server")
            and record.getMessage().startswith("{")
        ]
        access_records = [
            record
            for record in access_records
            if record.get("event") == "http_request"
            and record.get("request_id") == request_id
        ]
        if access_records:
            break
        time.sleep(0.01)

    assert status == 200
    assert len(access_records) == 1
    access_record = access_records[0]
    assert access_record == {
        "event": "http_request",
        "request_id": request_id,
        "method": "GET",
        "path": "/api/health",
        "status": 200,
        "duration_ms": access_record["duration_ms"],
    }
    assert isinstance(access_record["duration_ms"], (int, float))
    assert access_record["duration_ms"] >= 0
    assert secret not in json.dumps(access_record)


def test_health_tracks_active_sse_connection(server) -> None:
    assert _health(server)["events"]["sse"]["active_connections"] == 0
    parsed = urlparse(server.url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    response = None
    try:
        connection.request(
            "GET",
            "/api/events?timeout=0.5&heartbeat=0.1&limit=0&bootstrap=0",
            headers={"Accept": "text/event-stream", "Connection": "close"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type", "").startswith(
            "text/event-stream"
        )
        assert _health(server)["events"]["sse"]["active_connections"] == 1
        response.read()
    finally:
        if response is not None:
            response.close()
        connection.close()

    deadline = time.monotonic() + 1
    active_connections = None
    while time.monotonic() < deadline:
        active_connections = _health(server)["events"]["sse"][
            "active_connections"
        ]
        if active_connections == 0:
            break
        time.sleep(0.01)

    assert active_connections == 0
