"""Client-side ACKs require a complete server delivery binding."""

from app.alert_client import AlertClientState, AlertEventWorker


class RecordingApi:
    def __init__(self):
        self.calls = []

    def ack_alert_delivery(self, delivery_id, **payload):
        self.calls.append((delivery_id, payload))
        return {"status": "confirmed"}


def test_alert_worker_ack_includes_complete_use_evidence(tmp_path):
    worker = AlertEventWorker(
        "http://example.invalid",
        state=AlertClientState(tmp_path / "state.json"),
        client_id="client-1",
    )
    api = RecordingApi()
    worker._post_alert_ack(
        api,
        {
            "id": "evt-1",
            "event_id": "charge-1",
            "evidence": {"quality_evidence_id": "q-1"},
            "delivery": {
                "delivery_id": "delivery-1",
                "charge_event_id": "charge-1",
                "revision": 2,
                "connection_id": "connection-1",
                "started_at": "2026-10-01T00:00:00+00:00",
                "ended_at": "2026-10-01T00:00:30+00:00",
                "duration_seconds": 30,
            },
        },
    )
    assert len(api.calls) == 1
    delivery_id, payload = api.calls[0]
    assert delivery_id == "delivery-1"
    assert payload["charge_event_id"] == "charge-1"
    assert payload["revision"] == 2
    assert payload["ack_idempotency_key"] == "alert-ack:delivery-1:2"
    assert payload["evidence"]["schema_version"] == "alert-use-evidence.v1"
    assert payload["evidence"]["source_evidence"]["quality_evidence_id"] == "q-1"


def test_alert_worker_does_not_guess_a_delivery_for_free_alert(tmp_path):
    worker = AlertEventWorker("http://example.invalid", AlertClientState(tmp_path / "state.json"))
    api = RecordingApi()
    worker._post_alert_ack(api, {"id": "evt-free", "system_name": "Jita"})
    assert api.calls == []
