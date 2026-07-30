import httpx

from app.persistent_intel_client import PersistentIntelApiClient


def test_persistent_client_reuses_httpx_transport_for_json_requests():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"heartbeat": {"status": "running"}})

    client = PersistentIntelApiClient(
        "https://sentry.test",
        api_key="eve_secret",
    )
    client._http.close()
    client._http = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.post_heartbeat("client-1", "detector_client")
    finally:
        client.close()

    assert result["status"] == "running"
    assert requests[0].headers["authorization"] == "Bearer eve_secret"


def test_persistent_client_uses_same_transport_for_sse_events():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b"id: evt-1\n"
                b"event: alert\n"
                b'data: {"id": "alert-1", "system": "Tama"}\n\n'
            ),
        )

    client = PersistentIntelApiClient(
        "https://sentry.test",
        api_key="eve_secret",
    )
    client._http.close()
    client._http = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        events = list(client.iter_events(timeout=0, last_event_id="evt-0"))
    finally:
        client.close()

    assert events == [
        {
            "id": "evt-1",
            "event": "alert",
            "data": {"id": "alert-1", "system": "Tama"},
        }
    ]
    assert requests[0].headers["accept"] == "text/event-stream"
    assert requests[0].headers["authorization"] == "Bearer eve_secret"
    assert requests[0].headers["last-event-id"] == "evt-0"


def test_persistent_client_keeps_reading_after_sse_event_separator():
    def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b"event: bootstrap\n"
                b'data: {"active": []}\n\n'
                b"id: evt-2\n"
                b"event: alert\n"
                b'data: {"id": "alert-2", "system": "Tama"}\n\n'
            ),
        )

    client = PersistentIntelApiClient("https://sentry.test")
    client._http.close()
    client._http = httpx.Client(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        events = list(client.iter_events(timeout=0))
    finally:
        client.close()

    assert [event["event"] for event in events] == ["bootstrap", "alert"]
    assert events[1]["id"] == "evt-2"
