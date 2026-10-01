"""Sentry-side SeAT alert consumption protocol tests."""

import hashlib
import json
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.server.auth import AuthService
from app.server.auth_store import AuthRepository
from app.server.http_server import IntelHTTPServer
from app.server.seat_billing import SeatBillingRepository
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


def test_alert_consumption_is_closed_by_default(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    token = "seat-service-token-" + "x" * 40
    server = IntelHTTPServer(store, port=0, seat_integration_token=token)
    server.start()
    try:
        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/alert-grants",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            payload={},
        )
        assert status == 503
        assert payload["code"] == "alert_consumption_disabled"
    finally:
        server.stop()
        store.close()


def test_alert_grant_delivery_ack_is_idempotent(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    token = "seat-service-token-" + "x" * 40
    auth = AuthService(AuthRepository(store._connect), resolver=None, seat_auth_mode="enforce")
    user = auth.create_user("billing-pilot", "strong-password-123", role="member")
    account_id = str(uuid.uuid4())
    secret = "eve_seat_" + uuid.uuid4().hex
    key_id = _seat_key(auth, user["user_id"], account_id, secret)
    server = IntelHTTPServer(
        store,
        port=0,
        seat_integration_token=token,
        auth_service=auth,
        allow_alert_consumption=True,
    )
    server.start()
    service_headers = {"Authorization": f"Bearer {token}"}
    alert_headers = {"Authorization": f"Bearer {secret}"}
    grant_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    delivery_id = str(uuid.uuid4())
    try:
        grant_payload = {
            "operation_id": operation_id,
            "grant_id": grant_id,
            "account_id": account_id,
            "key_id": key_id,
            "price_version": "alert-v1-test",
            "unit_seconds": 60,
            "unit_price_minor": 7,
            "reserved_seconds": 60,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "protocol_version": 1,
        }
        status, grant = _request(
            f"{server.url}/api/v1/integrations/seat/alert-grants",
            method="POST", headers=service_headers, payload=grant_payload,
        )
        assert status == 201
        assert grant["remaining_seconds"] == 60
        status, replay = _request(
            f"{server.url}/api/v1/integrations/seat/alert-grants",
            method="POST", headers=service_headers, payload=grant_payload,
        )
        assert status == 200
        assert replay["idempotent_replay"] is True

        event_payload = {
            "charge_event_id": event_id,
            "wave_id": "wave-test",
            "revision": 1,
            "event_type": "alert.entered",
            "system_id": 30000142,
            "system_name": "Jita",
            "rule_version": "rules-v1",
            "eligibility": {
                "source_valid": True,
                "identification_trusted": True,
                "event_valid": True,
                "software_received": False,
            },
            "evidence": {"quality_evidence_id": "q-test"},
            "lifecycle": "eligible",
        }
        status, event = _request(
            f"{server.url}/api/v1/integrations/seat/alert-events",
            method="POST", headers=service_headers, payload=event_payload,
        )
        assert status == 201
        assert event["revision"] == 1

        delivery_payload = {
            "delivery_id": delivery_id,
            "charge_event_id": event_id,
            "revision": 1,
            "grant_id": grant_id,
            "account_id": account_id,
            "key_id": key_id,
            "connection_id": "conn-test",
            "client_version": "1.0.99",
            "ack_capability": "alert-ack.v1",
            "started_at": "2026-10-01T00:00:00+00:00",
            "ended_at": "2026-10-01T00:00:30+00:00",
        }
        status, delivery = _request(
            f"{server.url}/api/v1/integrations/seat/alert-deliveries",
            method="POST", headers=service_headers, payload=delivery_payload,
        )
        assert status == 201
        assert delivery["status"] == "sent"
        assert delivery["duration_seconds"] == 30
        assert delivery["billed_units"] == 1
        second_delivery = {**delivery_payload, "delivery_id": str(uuid.uuid4()), "connection_id": "conn-second"}
        status, second = _request(
            f"{server.url}/api/v1/integrations/seat/alert-deliveries",
            method="POST", headers=service_headers, payload=second_delivery,
        )
        assert status == 201
        assert second["status"] == "sent"
        assert store._auth_connection.execute(
            "SELECT remaining_seconds FROM seat_alert_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()[0] == 30

        ack_payload = {
            "charge_event_id": event_id,
            "revision": 1,
            "connection_id": "conn-test",
            "started_at": "2026-10-01T00:00:00+00:00",
            "ended_at": "2026-10-01T00:00:30+00:00",
            "evidence": {
                "schema_version": "alert-use-evidence.v1",
                "alert_id": "evt-client",
                "client_id": "client-1",
                "connection_id": "conn-test",
                "received_at": "2026-10-01T00:00:01+00:00",
                "ui_handled_at": "2026-10-01T00:00:02+00:00",
                "dedupe": "accepted",
                "ui_delivery": "alert_received",
            },
        }
        status, ack = _request(
            f"{server.url}/api/v1/alert-deliveries/{delivery_id}/ack",
            method="POST", headers={**alert_headers, "Idempotency-Key": "ack-1"}, payload=ack_payload,
        )
        assert status == 200
        assert ack["status"] == "confirmed"
        status, replay_ack = _request(
            f"{server.url}/api/v1/alert-deliveries/{delivery_id}/ack",
            method="POST", headers={**alert_headers, "Idempotency-Key": "ack-1"}, payload=ack_payload,
        )
        assert status == 200
        assert replay_ack["idempotent_replay"] is True

        status, page = _request(
            f"{server.url}/api/v1/integrations/seat/alert-events?limit=10",
            headers=service_headers,
        )
        assert status == 200
        assert page["protocol_version"] == 1
        assert page["events"][0]["charge_event_id"] == event_id
        status, deliveries = _request(
            f"{server.url}/api/v1/integrations/seat/alert-deliveries?limit=10",
            headers=service_headers,
        )
        assert status == 200
        assert deliveries["protocol_version"] == 1
        consumed = next(
            item for item in deliveries["deliveries"] if item["delivery_id"] == delivery_id
        )
        assert consumed["charge_event_id"] == event_id
        assert consumed["consumption_state"] == "consumed"
        assert consumed["ack_evidence"]["schema_version"] == "alert-use-evidence.v1"

        expiring_delivery = {
            **delivery_payload,
            "delivery_id": str(uuid.uuid4()),
            "started_at": "2026-10-01T00:01:00+00:00",
            "ended_at": "2026-10-01T00:01:10+00:00",
            "ack_deadline_at": "2020-01-01T00:00:00+00:00",
        }
        status, _expiring = _request(
            f"{server.url}/api/v1/integrations/seat/alert-deliveries",
            method="POST", headers=service_headers, payload=expiring_delivery,
        )
        assert status == 201
        released = SeatBillingRepository(store._connect).release_expired_consumptions(
            now="2026-10-01T00:02:00+00:00"
        )
        assert released[0]["state"] == "released"
        refunded = SeatBillingRepository(store._connect).refund_consumption(
            grant_id=grant_id,
            charge_event_id=event_id,
            revision=1,
            started_at="2026-10-01T00:00:00+00:00",
            ended_at="2026-10-01T00:00:30+00:00",
            reason="false positive",
            now="2026-10-01T00:03:00+00:00",
        )
        assert refunded["state"] == "refunded"
    finally:
        server.stop()
        auth.close()
        store.close()


def test_delivery_without_ack_capability_does_not_reserve_seconds(tmp_path):
    store = AuthTestStore(tmp_path / "intel.json")
    token = "seat-service-token-" + "x" * 40
    server = IntelHTTPServer(
        store,
        port=0,
        seat_integration_token=token,
        allow_alert_consumption=True,
    )
    server.start()
    service_headers = {"Authorization": f"Bearer {token}"}
    grant_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    account_id = str(uuid.uuid4())
    try:
        grant = {
            "operation_id": str(uuid.uuid4()),
            "grant_id": grant_id,
            "account_id": account_id,
            "price_version": "v1",
            "unit_seconds": 60,
            "unit_price_minor": 1,
            "reserved_seconds": 60,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "protocol_version": 1,
        }
        status, _ = _request(
            f"{server.url}/api/v1/integrations/seat/alert-grants",
            "POST",
            grant,
            service_headers,
        )
        assert status == 201
        event = {
            "charge_event_id": event_id,
            "revision": 1,
            "event_type": "alert.entered",
            "rule_version": "v1",
            "lifecycle": "eligible",
            "eligibility": {},
            "evidence": {},
        }
        status, _ = _request(
            f"{server.url}/api/v1/integrations/seat/alert-events",
            "POST",
            event,
            service_headers,
        )
        assert status == 201
        delivery = {
            "delivery_id": str(uuid.uuid4()),
            "charge_event_id": event_id,
            "revision": 1,
            "grant_id": grant_id,
            "account_id": account_id,
            "key_id": "key",
            "connection_id": "connection",
            "client_version": "old-client",
            "started_at": "2026-10-01T00:00:00+00:00",
            "ended_at": "2026-10-01T00:00:10+00:00",
        }
        status, payload = _request(
            f"{server.url}/api/v1/integrations/seat/alert-deliveries",
            "POST",
            delivery,
            service_headers,
        )
        assert status == 409
        assert payload["code"] == "client_ack_required"
        assert store._auth_connection.execute(
            "SELECT remaining_seconds FROM seat_alert_grants WHERE grant_id = ?",
            (grant_id,),
        ).fetchone()[0] == 60
    finally:
        server.stop()
        store.close()
