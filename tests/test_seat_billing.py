"""Sentry-side SeAT alert consumption protocol tests."""

import hashlib
import json
import sqlite3
import uuid
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.server.auth import AuthService
from app.server.auth_store import AuthRepository
from app.server.http_server import IntelHTTPServer
from app.server.seat_billing import SeatBillingRepository, migrate_seat_billing_schema
from tests.auth_test_store import AuthTestStore


def _request(url, method="GET", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request_headers = dict(headers or {})
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else {}
    except HTTPError as exc:
        body = exc.read().decode()
        return exc.code, json.loads(body) if body else {}


def _seat_key(auth, user_id, account_id, secret):
    key_id = str(uuid.uuid4())
    auth.repository.create_seat_integration_key(
        {
            "key_id": key_id,
            "account_id": account_id,
            "name": "billing test",
            "key_prefix": secret[:12],
            "key_hash": hashlib.sha256(secret.encode()).hexdigest(),
            "permissions_json": json.dumps(["alert"]),
            "protocol_version": 1,
            "status": "active",
            "created_at": "2026-10-01T00:00:00+00:00",
            "revoked_at": "",
            "revoked_reason": "",
        },
        str(uuid.uuid4()),
        "request-hash",
        auth._audit_record("seat-integration", key_id, "seat_key.created", {}),
    )
    auth.bind_seat_account(account_id, user_id, "test")
    return key_id


def test_authenticated_client_heartbeat_usage_is_idempotent(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    repository = SeatBillingRepository(store._connect)
    account_id = str(uuid.uuid4())
    record = {
        "account_id": account_id,
        "key_id": "key-online",
        "client_id": "client-online",
        "system_id": "30000142",
        "started_at": "2026-10-01T00:00:00+00:00",
        "ended_at": "2026-10-01T00:00:30+00:00",
        "duration_seconds": 30,
    }
    try:
        first = repository.record_client_usage(record)
        replay = repository.record_client_usage(record)
        page = repository.list_client_usage(limit=10)
        assert first["created"] is True
        assert replay["created"] is False
        assert page["usage"][0]["usage_id"] == first["usage"]["usage_id"]
        assert page["usage"][0]["duration_seconds"] == 30
        assert page["usage"][0]["system_id"] == "30000142"
    finally:
        store.close()


def test_client_usage_expands_to_overlapping_monitored_systems(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    repository = SeatBillingRepository(store._connect)
    account_id = str(uuid.uuid4())
    base = {
        "account_id": account_id,
        "key_id": "key-online",
        "client_id": "client-online",
        "started_at": "2026-10-01T00:00:00+00:00",
        "ended_at": "2026-10-01T00:00:30+00:00",
        "duration_seconds": 30,
    }
    try:
        for system_id, name in ((30000142, "Tama"), (30002813, "S-KSWL")):
            repository.record_monitor_contribution(
                {
                    "account_id": account_id,
                    "key_id": "key-monitor",
                    "client_id": f"monitor-{system_id}",
                    "system_id": system_id,
                    "system_name": name,
                    "primary_generation": 1,
                    **{key: base[key] for key in ("started_at", "ended_at", "duration_seconds")},
                }
            )
        first = repository.record_client_usage(base)
        replay = repository.record_client_usage(base)
        page = repository.list_client_usage(limit=10)
        assert first["created"] is True
        assert replay["created"] is False
        assert len(first["usages"]) == 2
        assert len(page["usage"]) == 2
        assert {item["system_id"] for item in page["usage"]} == {"30000142", "30002813"}
    finally:
        store.close()


def test_legacy_client_usage_table_gets_system_attribution_column():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE seat_client_usage (
            usage_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
            created_at TEXT NOT NULL,
            UNIQUE (client_id, started_at, ended_at)
        )
        """
    )
    try:
        migrate_seat_billing_schema(connection)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(seat_client_usage)")}
        assert "system_id" in columns

        repository = SeatBillingRepository(lambda: connection)
        result = repository.record_client_usage(
            {
                "account_id": "account-legacy",
                "key_id": "key-legacy",
                "client_id": "client-legacy",
                "system_id": "30000142",
                "started_at": "2026-10-01T00:00:00+00:00",
                "ended_at": "2026-10-01T00:00:30+00:00",
                "duration_seconds": 30,
            }
        )
        assert result["usage"]["system_id"] == "30000142"
    finally:
        connection.close()


def test_client_usage_export_is_service_token_protected(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    token = "seat-service-token-" + "x" * 40
    server = IntelHTTPServer(store, port=0, seat_integration_token=token)
    repository = SeatBillingRepository(store._connect)
    record = {
        "account_id": "seat-account",
        "key_id": "seat-key",
        "client_id": "client-1",
        "started_at": "2026-10-02T00:00:00+00:00",
        "ended_at": "2026-10-02T00:00:30+00:00",
        "duration_seconds": 30,
    }
    try:
        saved = repository.record_client_usage(record)
        server.start()
        status, unauthorized = _request(
            f"{server.url}/api/v1/integrations/seat/client-usage",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert status == 401
        assert unauthorized["code"] == "seat_integration_unauthorized"
        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/client-usage?limit=10",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status == 200
        assert payload["rule_version"] == "client-heartbeat.v1"
        assert payload["usage"][0]["usage_id"] == saved["usage"]["usage_id"]
        assert payload["usage"][0]["duration_seconds"] == 30
    finally:
        server.stop()
        store.close()


def test_alert_consumption_gate_is_persisted_and_service_token_controlled(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    token = "seat-service-token-" + "x" * 40
    server = IntelHTTPServer(store, port=0, seat_integration_token=token)
    server.start()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/alert-consumption",
            headers=headers,
        )
        assert status == 200
        assert payload["enabled"] is False

        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/alert-consumption",
            method="PUT",
            headers=headers,
            payload={"enabled": True},
        )
        assert status == 200
        assert payload["enabled"] is True

        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/alert-consumption",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert status == 401
        assert payload["code"] == "seat_integration_unauthorized"

        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/alert-consumption",
            method="PUT",
            headers=headers,
            payload={"enabled": False},
        )
        assert status == 200
        assert payload["enabled"] is False
    finally:
        server.stop()
        store.close()


def test_primary_monitor_contribution_export_is_idempotent(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    token = "seat-service-token-" + "x" * 40
    server = IntelHTTPServer(store, port=0, seat_integration_token=token)
    repository = SeatBillingRepository(store._connect)
    record = {
        "account_id": "seat-account",
        "key_id": "seat-key",
        "client_id": "detector-1",
        "system_id": 30004759,
        "system_name": "S-KSWL",
        "primary_generation": 7,
        "started_at": "2026-10-02T00:00:00+00:00",
        "ended_at": "2026-10-02T00:00:10+00:00",
        "duration_seconds": 10,
        "created_at": "2026-10-02T00:00:10+00:00",
    }
    try:
        first = repository.record_monitor_contribution(record)
        replay = repository.record_monitor_contribution(record)
        assert first["created"] is True
        assert replay["created"] is False
        server.start()
        status, unauthorized = _request(
            f"{server.url}/api/v1/integrations/seat/monitor-contributions",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert status == 401
        assert unauthorized["code"] == "seat_integration_unauthorized"
        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/monitor-contributions?limit=10",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status == 200
        assert payload["rule_version"] == "primary-presence.v1"
        assert payload["contributions"][0]["contribution_id"] == first["contribution"]["contribution_id"]
        assert payload["contributions"][0]["duration_seconds"] == 10
        status, empty = _request(
            f"{server.url}/api/v1/integrations/seat/monitor-contributions?after={quote(payload['next_cursor'])}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status == 200
        assert empty["contributions"] == []
    finally:
        server.stop()
        store.close()
