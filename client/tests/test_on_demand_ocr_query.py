"""HTTP contract coverage for server-driven one-shot OCR queries."""

from app.intel_client import IntelApiClient


def test_intel_api_client_forwards_ocr_query_id_with_empty_names():
    class RecordingClient(IntelApiClient):
        def __init__(self):
            super().__init__("http://example.invalid")
            self.payload = None

        def _request(self, method, path, payload=None, params=None):
            _ = method, path, params
            self.payload = payload
            return {"refreshed": 0}

    api = RecordingClient()

    api.post_ocr_snapshot(
        client_id="detector-client:test:pilot-a",
        source_instance="EVE - Pilot A",
        system_name="S-KSWL",
        names=[],
        query_id="ocrq_abc123",
    )

    assert api.payload["names"] == []
    assert api.payload["query_id"] == "ocrq_abc123"


def test_intel_api_client_preserves_heartbeat_commands():
    class CommandClient(IntelApiClient):
        def __init__(self):
            super().__init__("http://example.invalid")

        def _request(self, method, path, payload=None, params=None):
            _ = method, path, payload, params
            return {
                "heartbeat": {"client_id": "detector-client:test"},
                "commands": [
                    {
                        "command": "ocr_query",
                        "query_id": "ocrq_abc123",
                    }
                ],
            }

    result = CommandClient().post_heartbeat(
        client_id="detector-client:test",
        client_type="detector_client",
    )

    assert result["client_id"] == "detector-client:test"
    assert result["commands"][0]["query_id"] == "ocrq_abc123"
