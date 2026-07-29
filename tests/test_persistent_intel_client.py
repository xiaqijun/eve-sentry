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
