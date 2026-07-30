"""Tests for the public liveness and storage readiness probes."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.server.auth import AuthService
from app.server.auth_store import AuthRepository
from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore
from tests.auth_test_store import AuthTestStore


def _request_json(url: str) -> tuple[int, dict]:
    request = Request(url, method="GET")
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}
    with response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body) if body else {}


def test_livez_and_readyz_bypass_enforced_authentication(tmp_path) -> None:
    store = AuthTestStore(tmp_path / "intel.json")
    auth = AuthService(AuthRepository(store._connect), object(), enforce_requests=True)
    server = IntelHTTPServer(store, port=0, auth_service=auth)
    server.start()
    try:
        assert _request_json(f"{server.url}/api/livez") == (200, {"ok": True})
        assert _request_json(f"{server.url}/api/readyz") == (
            200,
            {"ok": True, "checks": {"storage": {"ok": True}}},
        )
        protected_status, _ = _request_json(f"{server.url}/api/intel")
        assert protected_status == 401
    finally:
        server.stop()
        store.close()


def test_readyz_probes_json_storage(tmp_path) -> None:
    store = IntelStore(tmp_path / "healthy.json")
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, payload = _request_json(f"{server.url}/api/readyz")

        assert status == 200
        assert payload == {"ok": True, "checks": {"storage": {"ok": True}}}
    finally:
        server.stop()
        store.close()


def test_readyz_checks_json_storage_without_leaking_its_path(tmp_path) -> None:
    secret_path = tmp_path / "private-storage-location.json"
    store = IntelStore(secret_path)
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, payload = _request_json(f"{server.url}/api/readyz")

        assert status == 200
        assert payload == {"ok": True, "checks": {"storage": {"ok": True}}}
        assert str(secret_path) not in json.dumps(payload)
        assert secret_path.name not in json.dumps(payload)
    finally:
        server.stop()
        store.close()


def test_readyz_returns_503_without_leaking_storage_errors() -> None:
    class FailingPostgresStore:
        _postgres_dsn = "postgresql://admin:super-secret@db.internal/eve"
        _postgres_safe_dsn = "postgresql://***@db.internal/eve"

        def _connect(self):
            raise RuntimeError(self._postgres_dsn)

    server = IntelHTTPServer(FailingPostgresStore(), port=0)
    server.start()
    try:
        live_status, live_payload = _request_json(f"{server.url}/api/livez")
        ready_status, ready_payload = _request_json(f"{server.url}/api/readyz")

        assert (live_status, live_payload) == (200, {"ok": True})
        assert ready_status == 503
        assert ready_payload == {
            "ok": False,
            "checks": {"storage": {"ok": False}},
        }
        serialized = json.dumps(ready_payload)
        assert "super-secret" not in serialized
        assert "db.internal" not in serialized
    finally:
        server.stop()
