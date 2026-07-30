import http.client
import json
import time
from urllib.parse import urlparse

import pytest

from app.server.auth import AuthService
from app.server.auth_store import AuthRepository
from app.server.http_server import (
    MAX_JSON_BODY_BYTES,
    IntelHTTPServer,
    IntelRequestHandler,
)
from app.server.intel_store import IntelStore
from tests.auth_test_store import AuthTestStore


def _request(server, method, path, headers=None, body=None):
    parsed = urlparse(server.url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    connection.putrequest(method, path)
    for name, value in (headers or {}).items():
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    raw = response.read()
    result = (
        response.status,
        response.getheader("Content-Type", ""),
        json.loads(raw.decode("utf-8")) if raw else {},
    )
    connection.close()
    return result


@pytest.fixture
def server(tmp_path):
    instance = IntelHTTPServer(IntelStore(tmp_path / "intel.json"), port=0)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.mark.parametrize(
    ("headers", "expected_error"),
    [
        ({}, "Content-Length header is required"),
        ({"Content-Length": "invalid"}, "Content-Length must be"),
        ({"Content-Length": "+1"}, "Content-Length must be"),
        ({"Content-Length": "-1"}, "Content-Length must be"),
    ],
)
@pytest.mark.parametrize(
    "path",
    ["/api/observations", "/api/alerts/missing/ack"],
)
def test_json_routes_reject_missing_or_invalid_content_length(
    server,
    path,
    headers,
    expected_error,
):
    status, content_type, payload = _request(server, "POST", path, headers)

    assert status == 400
    assert content_type.startswith("application/json")
    assert expected_error in payload["error"]


@pytest.mark.parametrize(
    "path",
    ["/api/observations", "/api/alerts/missing/ack"],
)
def test_json_routes_reject_bodies_larger_than_one_mebibyte(server, path):
    status, _, payload = _request(
        server,
        "POST",
        path,
        {"Content-Length": str(MAX_JSON_BODY_BYTES + 1)},
    )

    assert status == 413
    assert str(MAX_JSON_BODY_BYTES) in payload["error"]


def test_json_route_accepts_body_at_one_mebibyte_limit(server):
    prefix = b'{"note":"'
    suffix = b'"}'
    body = prefix + (b"x" * (MAX_JSON_BODY_BYTES - len(prefix) - len(suffix))) + suffix

    status, _, payload = _request(
        server,
        "POST",
        "/api/alerts/missing/ack",
        {"Content-Length": str(len(body)), "Content-Type": "application/json"},
        body,
    )

    assert len(body) == MAX_JSON_BODY_BYTES
    assert status == 404
    assert payload == {"error": "alert not found"}


def test_invalid_utf8_body_returns_json_error_and_server_remains_available(server):
    status, _, payload = _request(
        server,
        "POST",
        "/api/observations",
        {"Content-Length": "1", "Content-Type": "application/json"},
        b"\xff",
    )

    assert status == 400
    assert payload == {"error": "request body must be valid UTF-8"}
    health_status, _, _ = _request(server, "GET", "/api/health")
    assert health_status == 200


def test_auth_json_route_preserves_payload_too_large_status(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    auth = AuthService(AuthRepository(store._connect), object())
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    try:
        status, _, payload = _request(
            server,
            "POST",
            "/api/v1/auth/login",
            {"Content-Length": str(MAX_JSON_BODY_BYTES + 1)},
        )

        assert status == 413
        assert str(MAX_JSON_BODY_BYTES) in payload["error"]
    finally:
        server.stop()
        store.close()


@pytest.mark.parametrize(
    "path",
    [
        "/api/reports",
        "/api/observations",
        "/api/alerts",
        "/api/events",
        "/api/v1/reports",
        "/api/v1/observations",
        "/api/v1/alerts",
        "/api/v1/events",
    ],
)
def test_all_limit_queries_reject_values_above_one_thousand(server, path):
    status, _, payload = _request(server, "GET", f"{path}?limit=1001")

    assert status == 400
    assert payload == {"error": "limit must not exceed 1000"}


def test_limit_query_accepts_one_thousand(server):
    for path in ("/api/reports", "/api/v1/reports"):
        status, _, payload = _request(server, "GET", f"{path}?limit=1000")
        assert status == 200
        assert payload["count"] == 0


@pytest.mark.parametrize("path", ["/api/events", "/api/v1/events"])
@pytest.mark.parametrize(
    ("parameter", "value", "expected_error"),
    [
        ("timeout", "300.01", "timeout must not exceed 300"),
        ("heartbeat", "60.01", "heartbeat must not exceed 60"),
        ("timeout", "nan", "timeout must be finite"),
        ("timeout", "inf", "timeout must be finite"),
        ("heartbeat", "-inf", "heartbeat must be finite"),
    ],
)
def test_sse_queries_reject_unbounded_or_non_finite_values(
    server,
    path,
    parameter,
    value,
    expected_error,
):
    status, _, payload = _request(server, "GET", f"{path}?{parameter}={value}")

    assert status == 400
    assert payload == {"error": expected_error}


@pytest.mark.parametrize("path", ["/api/events", "/api/v1/events"])
def test_sse_zero_timeout_and_heartbeat_remain_valid(server, path):
    started_at = time.monotonic()
    status, content_type, _ = _request(
        server,
        "GET",
        f"{path}?timeout=0&heartbeat=0&limit=0&bootstrap=0",
    )

    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert time.monotonic() - started_at < 1


def test_resource_limit_boundaries_are_inclusive():
    handler = object.__new__(IntelRequestHandler)

    assert handler._parse_optional_int("1000") == 1000
    assert handler._parse_optional_float_param("300", "timeout") == 300
    assert handler._parse_optional_float_param("60", "heartbeat") == 60
