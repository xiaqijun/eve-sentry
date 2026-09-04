import threading
import time

from PyQt6.QtCore import Qt

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


def test_reliable_uploader_nonblocking_shutdown_closes_after_request_returns():
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class Client:
        def post_ocr_snapshot(self, **_payload):
            started.set()
            assert release.wait(2)
            return {"ok": True}

        def post_heartbeat(self, **_payload):
            return {"ok": True}

        def close(self):
            closed.set()

    manager = ReliableUploadManager(Client())
    manager.submit_snapshot(
        "window-1",
        {
            "client_id": "window-1",
            "source_instance": "EVE - A",
            "system_name": "Tama",
            "names": [],
        },
    )
    assert started.wait(1)

    manager.shutdown(timeout=0)

    assert not closed.is_set()
    release.set()
    assert closed.wait(1)


def test_reliable_uploader_restores_latest_snapshot_after_restart(tmp_path):
    state_path = tmp_path / "offline-snapshots.json"
    failed = threading.Event()

    class OfflineClient:
        def post_ocr_snapshot(self, **_payload):
            failed.set()
            raise transient_error()

        def post_heartbeat(self, **_payload):
            raise transient_error()

    first = ReliableUploadManager(
        OfflineClient(),
        state_path=state_path,
        random_source=lambda: 0.5,
    )
    try:
        first.submit_snapshot(
            "window-1",
            {
                "client_id": "window-1",
                "source_instance": "EVE - A",
                "system_name": "Tama",
                "names": ["Latest Pilot"],
                "api_key": "do-not-persist",
            },
            ttl=60,
        )
        assert failed.wait(1)
        assert state_path.exists()
        assert "do-not-persist" not in state_path.read_text(encoding="utf-8")
    finally:
        first.shutdown()

    calls = []

    class OnlineClient:
        def post_ocr_snapshot(self, **payload):
            calls.append(payload)
            return {"ok": True}

        def post_heartbeat(self, **_payload):
            return {"ok": True}

    second = ReliableUploadManager(OnlineClient(), state_path=state_path)
    try:
        assert wait_until(lambda: second.pending_snapshot_count() == 0)
        assert calls and calls[0]["names"] == ["Latest Pilot"]
    finally:
        second.shutdown()
    assert not state_path.exists()


def test_reliable_uploader_drops_expired_disk_snapshot(tmp_path):
    state_path = tmp_path / "offline-snapshots.json"

    class OfflineClient:
        def post_ocr_snapshot(self, **_payload):
            raise transient_error()

        def post_heartbeat(self, **_payload):
            raise transient_error()

    manager = ReliableUploadManager(OfflineClient(), state_path=state_path)
    manager.submit_snapshot(
        "window-1",
        {
            "client_id": "window-1",
            "source_instance": "EVE - A",
            "system_name": "Tama",
            "names": [],
        },
        ttl=0.1,
    )
    time.sleep(0.2)
    manager.shutdown()

    restored = ReliableUploadManager(OfflineClient(), state_path=state_path)
    try:
        assert restored.pending_snapshot_count() == 0
    finally:
        restored.shutdown()


def test_reliable_uploader_prioritizes_presence_without_replacing_ocr(tmp_path):
    first_failed = threading.Event()
    calls = []

    class Client:
        def __init__(self):
            self.online = False

        def post_ocr_snapshot(self, **payload):
            calls.append(("ocr", payload))
            if not self.online:
                first_failed.set()
                raise transient_error()
            return {"ok": True}

        def post_hostile_presence(self, **payload):
            calls.append(("presence", payload))
            if not self.online:
                raise transient_error()
            return {"ok": True}

    client = Client()
    manager = ReliableUploadManager(
        client,
        state_path=tmp_path / "uploads.json",
        random_source=lambda: 0.5,
    )
    try:
        manager.submit_snapshot(
            "window-1",
            {
                "client_id": "window-1",
                "source_instance": "EVE - A",
                "system_name": "Tama",
                "names": ["Enemy Pilot"],
            },
        )
        assert first_failed.wait(1)
        assert manager.state == "reconnecting"

        manager.submit_presence(
            "window-1",
            {
                "client_id": "window-1",
                "source_instance": "EVE - A",
                "system_name": "Tama",
                "hostile_icon_count": 0,
            },
        )
        client.online = True
        with manager._condition:
            manager._retry_at = 0.0
            manager._condition.notify_all()

        assert wait_until(
            lambda: manager.pending_presence_count() == 0
            and manager.pending_snapshot_count() == 0
        )
        assert [kind for kind, _payload in calls] == ["ocr", "presence", "ocr"]
        presence_payload = calls[1][1]
        snapshot_payload = calls[2][1]
        assert presence_payload["hostile_icon_count"] == 0
        assert presence_payload["sequence"] == 1
        assert snapshot_payload["names"] == ["Enemy Pilot"]
        assert snapshot_payload["sequence"] == 1
        assert presence_payload["snapshot_id"] != snapshot_payload["snapshot_id"]
    finally:
        manager.shutdown()


def test_reliable_uploader_prioritizes_presence_then_heartbeat_then_ocr(tmp_path):
    calls = []

    class Client:
        def post_hostile_presence(self, **_payload):
            calls.append("presence")
            return {"ok": True}

        def post_heartbeat(self, **_payload):
            calls.append("heartbeat")
            return {"ok": True}

        def post_ocr_snapshot(self, **_payload):
            calls.append("ocr")
            return {"ok": True}

    manager = ReliableUploadManager(
        Client(),
        state_path=tmp_path / "uploads.json",
    )
    try:
        with manager._condition:
            manager.submit_snapshot(
                "window-1",
                {
                    "client_id": "window-1",
                    "source_instance": "EVE - A",
                    "system_name": "Tama",
                    "names": ["Enemy Pilot"],
                },
            )
            manager.submit_heartbeat({"client_id": "detector-1"})
            manager.submit_presence(
                "window-1",
                {
                    "client_id": "window-1",
                    "source_instance": "EVE - A",
                    "system_name": "Tama",
                    "hostile_icon_count": 1,
                },
            )

        assert wait_until(lambda: len(calls) == 3)
        assert calls == ["presence", "heartbeat", "ocr"]
    finally:
        manager.shutdown()


def test_reliable_uploader_exposes_heartbeat_commands_to_ui(tmp_path):
    class Client:
        def post_heartbeat(self, **payload):
            return {
                "client_id": payload["client_id"],
                "commands": [
                    {
                        "command": "ocr_query",
                        "query_id": "ocrq_abc123",
                    }
                ],
            }

    manager = ReliableUploadManager(
        Client(),
        state_path=tmp_path / "uploads.json",
    )
    uploaded = []
    manager.heartbeat_uploaded.connect(
        uploaded.append,
        Qt.ConnectionType.DirectConnection,
    )
    try:
        manager.submit_heartbeat(
            {"client_id": "detector-client:test"},
            {"kind": "heartbeat"},
        )

        assert wait_until(lambda: bool(uploaded))
        assert uploaded[0]["response"]["commands"][0]["query_id"] == (
            "ocrq_abc123"
        )
    finally:
        manager.shutdown()


def test_reliable_uploader_coalesces_presence_to_one_item_per_eight_windows(tmp_path):
    first_failed = threading.Event()

    class OfflineClient:
        def post_hostile_presence(self, **_payload):
            first_failed.set()
            raise transient_error()

        def post_ocr_snapshot(self, **_payload):
            raise transient_error()

    manager = ReliableUploadManager(
        OfflineClient(),
        state_path=tmp_path / "uploads.json",
        random_source=lambda: 0.5,
    )
    try:
        for generation in range(5):
            for window_index in range(8):
                client_id = f"window-{window_index}"
                manager.submit_presence(
                    client_id,
                    {
                        "client_id": client_id,
                        "source_instance": f"EVE - Pilot {window_index}",
                        "system_name": "Tama",
                        "hostile_icon_count": generation,
                    },
                )

        assert first_failed.wait(1)
        assert wait_until(lambda: manager.pending_presence_count() == 8)
        assert manager.state == "reconnecting"
        assert manager.pending_presence_count() == 8
        assert manager.pending_snapshot_count() == 0
        assert {
            upload.payload["hostile_icon_count"]
            for upload in manager._presence.values()
        } == {4}
    finally:
        manager.shutdown()
