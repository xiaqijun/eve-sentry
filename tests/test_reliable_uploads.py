import threading
import time

from app.intel_client import IntelApiError
from app.ui.reliable_uploads import ReliableUploadManager


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def transient_error(message="offline"):
    error = IntelApiError(message)
    error.transient = True
    error.status_code = None
    return error


def test_reliable_uploader_replaces_offline_snapshot_with_latest_state():
    first_failed = threading.Event()
    calls = []

    class Client:
        def post_ocr_snapshot(self, **payload):
            calls.append(payload)
            if len(calls) == 1:
                first_failed.set()
                raise transient_error()
            return {"ok": True}

        def post_heartbeat(self, **_payload):
            return {"ok": True}

    manager = ReliableUploadManager(Client(), random_source=lambda: 0.5)
    try:
        manager.submit_snapshot(
            "window-1",
            {
                "client_id": "window-1",
                "source_instance": "EVE - A",
                "system_name": "Tama",
                "names": ["Old Pilot"],
            },
        )
        assert first_failed.wait(1)
        manager.submit_snapshot(
            "window-1",
            {
                "client_id": "window-1",
                "source_instance": "EVE - A",
                "system_name": "Tama",
                "names": [],
            },
        )
        assert wait_until(lambda: manager.pending_snapshot_count() == 0)
        assert calls[0]["names"] == ["Old Pilot"]
        assert calls[-1]["names"] == []
        assert len(calls) == 2
        assert calls[-1]["sequence"] == 2
        assert calls[-1]["snapshot_id"]
    finally:
        manager.shutdown()


def test_reliable_uploader_reports_authentication_failure_without_retrying():
    calls = []

    class Client:
        def post_ocr_snapshot(self, **payload):
            calls.append(payload)
            error = IntelApiError("revoked")
            error.status_code = 401
            error.transient = False
            raise error

        def post_heartbeat(self, **_payload):
            return {"ok": True}

    manager = ReliableUploadManager(Client())
    try:
        manager.submit_snapshot(
            "window-1",
            {
                "client_id": "window-1",
                "source_instance": "EVE - A",
                "system_name": "Tama",
                "names": [],
            },
        )
        assert wait_until(lambda: manager.state == "authentication_failed")
        time.sleep(0.05)
        assert len(calls) == 1
    finally:
        manager.shutdown()
