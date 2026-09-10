"""Concurrency and legacy cursor regressions for the shared SSE cache."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.request import Request, urlopen

import pytest

from app.server import http_server
from app.server.event_cache import ActiveEventSnapshot
from app.server.http_server import IntelHTTPServer, IntelRequestHandler
from app.server.intel_store import IntelReport, IntelStore
from app.server.postgres_store import PostgreSQLIntelStore


def test_fresh_snapshot_reads_do_not_acquire_builder_lock_and_are_isolated():
    class ForbiddenLock:
        def acquire(self, **kwargs):
            pytest.fail("a cache hit must not acquire the builder lock")

    entry = ActiveEventSnapshot(
        http_server._event_stream_generation(), time.monotonic(), 7,
        ([{"metadata": {"names": ["Pilot"]}}], [], []),
        {"evt_report": (10, "report")},
    )
    store = SimpleNamespace(_sse_active_event_cache={"entry": entry, "lock": ForbiddenLock()})
    handler = object.__new__(IntelRequestHandler)
    cursors = {"obsolete": (0, "obsolete")}
    first = handler._cached_active_event_snapshot(store, report_cursors=cursors)
    assert first[3:] == (7, True)
    assert cursors == {"evt_report": (10, "report")}
    first[0][0]["metadata"]["names"].append("mutation")
    cursors.clear()
    second = handler._cached_active_event_snapshot(store, minimum_state_event_seq=7)
    assert second[0][0]["metadata"]["names"] == ["Pilot"]
    assert entry.report_cursors == {"evt_report": (10, "report")}


def test_concurrent_cold_readers_share_one_build_and_do_not_publish_empty_state():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def read_snapshot():
        calls.append(True)
        started.set()
        assert release.wait(3)
        return [], [], 11

    store = SimpleNamespace(read_active_event_snapshot=read_snapshot)
    handler = object.__new__(IntelRequestHandler)
    handler._build_active_event_state = lambda *args, **kwargs: ([], [], [])
    with ThreadPoolExecutor(max_workers=10) as pool:
        builder = pool.submit(handler._cached_active_event_snapshot, store)
        try:
            assert started.wait(1)
            readers = [pool.submit(handler._cached_active_event_snapshot, store) for _ in range(9)]
            results = [future.result(timeout=1) for future in readers]
            assert all(result[3:] == (0, False) for result in results)
            assert calls == [True]
        finally:
            release.set()
        assert builder.result(timeout=1)[3:] == (11, True)
    assert handler._cached_active_event_snapshot(store)[3:] == (11, True)
    assert calls == [True]


@pytest.mark.parametrize("reason", ["generation", "ttl", "watermark"])
def test_snapshot_refreshes_on_each_invalidation_reason(reason):
    generation = http_server._event_stream_generation()
    old = ActiveEventSnapshot(
        generation - 1 if reason == "generation" else generation,
        time.monotonic() - 2 if reason == "ttl" else time.monotonic(),
        1, ([{"id": "old"}], [], []),
    )
    calls = []

    def read_snapshot():
        calls.append(True)
        return [], [], 2

    store = SimpleNamespace(
        _sse_active_event_cache={"entry": old, "lock": threading.Lock()},
        read_active_event_snapshot=read_snapshot,
    )
    handler = object.__new__(IntelRequestHandler)
    handler._build_active_event_state = lambda *args, **kwargs: ([], [], [])
    result = handler._cached_active_event_snapshot(
        store, minimum_state_event_seq=2 if reason == "watermark" else 0,
    )
    assert result == ([], [], [], 2, True)
    assert calls == [True]


def test_busy_refresh_does_not_pair_new_watermark_with_old_payload(monkeypatch):
    monkeypatch.setattr(http_server, "_ACTIVE_EVENT_SNAPSHOT_LOCK_WAIT_SECONDS", 0.01)
    old = ActiveEventSnapshot(
        http_server._event_stream_generation(), time.monotonic(), 1,
        ([{"hostile_count": 1}], [], []), {"evt_old": (1, "old")},
    )
    lock = threading.Lock()
    store = SimpleNamespace(_sse_active_event_cache={"entry": old, "lock": lock})
    handler = object.__new__(IntelRequestHandler)
    with lock:
        result = handler._cached_active_event_snapshot(store, minimum_state_event_seq=2)
    assert result[0] == [{"hostile_count": 1}]
    assert result[3:] == (1, False)
    assert store._sse_active_event_cache["entry"] is old


def test_failed_refresh_keeps_last_entry_and_releases_lock_for_retry():
    old = ActiveEventSnapshot(-1, 0, 1, ([{"id": "old"}], [], []))
    calls = []

    def read_snapshot():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("database unavailable")
        return [], [], 2

    store = SimpleNamespace(
        _sse_active_event_cache={"entry": old, "lock": threading.Lock()},
        read_active_event_snapshot=read_snapshot,
    )
    handler = object.__new__(IntelRequestHandler)
    handler._build_active_event_state = lambda *args, **kwargs: ([], [], [])
    with pytest.raises(RuntimeError, match="database unavailable"):
        handler._cached_active_event_snapshot(store)
    assert store._sse_active_event_cache["entry"] is old
    assert handler._cached_active_event_snapshot(store) == ([], [], [], 2, True)


def test_snapshot_copy_runs_after_releasing_builder_lock():
    cache = {"entry": None, "lock": threading.Lock()}

    class CheckedDict(dict):
        def __deepcopy__(self, memo):
            assert cache["lock"].acquire(blocking=False)
            cache["lock"].release()
            return dict(self)

    store = SimpleNamespace(
        _sse_active_event_cache=cache,
        read_active_event_snapshot=lambda: ([], [], 1),
    )
    handler = object.__new__(IntelRequestHandler)
    handler._build_active_event_state = lambda *args, **kwargs: ([CheckedDict(id="a")], [], [])
    assert handler._cached_active_event_snapshot(store) == ([{"id": "a"}], [], [], 1, True)


def test_change_during_build_invalidates_the_completed_snapshot(monkeypatch):
    generation = [1]
    calls = []
    monkeypatch.setattr(http_server, "_event_stream_generation", lambda: generation[0])

    def read_snapshot():
        calls.append(True)
        watermark = generation[0]
        generation[0] = 2
        return [], [], watermark

    store = SimpleNamespace(read_active_event_snapshot=read_snapshot)
    handler = object.__new__(IntelRequestHandler)
    handler._build_active_event_state = lambda *args, **kwargs: ([], [], [])
    assert handler._cached_active_event_snapshot(store)[3:] == (1, True)
    assert handler._cached_active_event_snapshot(store)[3:] == (2, True)
    assert len(calls) == 2


def test_snapshot_refresh_replaces_report_index_after_clear():
    report = IntelReport(report_id="active", system="HB-FSO", names=["Pilot"], stream_position=5)
    calls = []

    def read_snapshot():
        calls.append(True)
        return [], [report] if len(calls) == 1 else [], len(calls)

    store = SimpleNamespace(
        read_active_event_snapshot=read_snapshot,
        _report_stream_cursor=IntelStore._report_stream_cursor,
    )
    handler = object.__new__(IntelRequestHandler)
    handler._build_active_event_state = lambda *args, **kwargs: ([], [], [])
    cursors = {}
    handler._cached_active_event_snapshot(store, report_cursors=cursors)
    assert cursors == {"evt_active": (5, "active")}
    result = handler._cached_active_event_snapshot(
        store, minimum_state_event_seq=2, report_cursors=cursors,
    )
    assert result[3:] == (2, True)
    assert cursors == {}
    assert store._sse_active_event_cache["entry"].report_cursors == {}


@pytest.mark.parametrize("cursor", ["", "2026-09-09T09:00:00+00:00", "presence_old", "state:7"])
def test_fresh_and_legacy_sse_connections_reuse_snapshot_report_index(tmp_path, monkeypatch, cursor):
    report = IntelReport(report_id="active", system="HB-FSO", names=["Pilot"], stream_position=5)
    builds = []

    class SnapshotStore(IntelStore):
        def read_active_event_snapshot(self):
            builds.append(True)
            return [], [report], 7

        def list_intel_event_page(self, **kwargs):
            return []

        def resolve_alert_stream_cursor(self, alert_id):
            pytest.fail("SSE must use the cached report index or timestamp directly")

    alert = {
        "id": "evt_active", "system_name": "HB-FSO", "score": 100,
        "level": "critical", "created_at": "2026-09-09T10:00:00+00:00",
    }
    monkeypatch.setattr(
        IntelRequestHandler, "_build_active_event_state",
        lambda *args, **kwargs: ([], [dict(alert)], []),
    )
    store = SnapshotStore(tmp_path / "intel.json", systems={}, links=[])
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        for _ in range(2):
            request = Request(
                f"{server.url}/api/v1/events?timeout=0&bootstrap=1",
                headers={"Last-Event-ID": cursor} if cursor else {},
            )
            with urlopen(request, timeout=2) as response:
                body = response.read().decode()
            assert "event: bootstrap\n" in body
            assert "event: alert\n" in body
            assert "id: state:7" in body
            payloads = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
            assert any(payload.get("id") == "evt_active" for payload in payloads)
        assert builds == [True]
    finally:
        server.stop()
        store.close()


@pytest.mark.parametrize("found", [True, False])
@pytest.mark.parametrize("time_field", ["created_at", "seen_at", None])
def test_standard_postgres_resume_never_reads_hot_history_or_scores(found, time_field):
    report = IntelReport(
        report_id="historical", system="HB-FSO", names=["Pilot"],
        stream_position=17, received_at="2026-09-09T10:00:00+00:00",
        metadata={
            "generated_alert": {time_field: "2026-09-09T09:00:00+00:00"}
            if time_field else {},
        },
    )
    store = object.__new__(PostgreSQLIntelStore)
    store._reports_snapshot = lambda: pytest.fail("must not acquire the hot history lock")
    store._alert_from_report = lambda report: pytest.fail("must not score on resume")
    store._report_for_alert_id = lambda alert_id: report if found else None
    handler = object.__new__(IntelRequestHandler)
    handler.headers = {"Last-Event-ID": "evt_historical"}
    handler._store = lambda: store
    result = handler._event_stream_cursor("")
    if found:
        timestamp = "2026-09-09T09:00:00+00:00" if time_field else report.received_at
        assert result == (timestamp, "evt_historical", True, (17, "historical"))
    else:
        assert result == ("", "", False, None)
