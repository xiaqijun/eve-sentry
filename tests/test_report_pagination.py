"""Tests for opt-in keyset pagination of report history."""

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


def _add(store, index: int, *, source: str = "manual", system: str = "Tama"):
    timestamp = f"2026-07-30T12:00:{index:02d}+00:00"
    return store.add_observation(
        {
            "source": source,
            "system_name": system,
            "names": [f"Pilot {index}"],
            "seen_at": timestamp,
            "received_at": timestamp,
        }
    )


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


def test_in_memory_report_pages_are_stable_and_non_overlapping(tmp_path) -> None:
    store = IntelStore(tmp_path / "intel.json")
    observations = [_add(store, index) for index in range(1, 6)]
    expected = [item.observation_id for item in reversed(observations)]
    try:
        first = store.report_page(limit=2)
        second = store.report_page(cursor=first["next_cursor"], limit=2)
        third = store.report_page(cursor=second["next_cursor"], limit=2)

        ids = [
            item["id"]
            for page in (first, second, third)
            for item in page["reports"]
        ]
        assert ids == expected
        assert len(ids) == len(set(ids))
        assert first["next_cursor"]
        assert second["next_cursor"]
        assert third["next_cursor"] == ""
    finally:
        store.close()


def test_observation_page_filters_and_rejects_invalid_cursor(tmp_path) -> None:
    store = IntelStore(tmp_path / "intel.json")
    _add(store, 1, source="manual", system="Tama")
    expected = _add(store, 2, source="intel_channel", system="Jita")
    try:
        page = store.observation_page(
            limit=5,
            source="INTEL_CHANNEL",
            system="jita",
            name="pilot 2",
        )

        assert [item["id"] for item in page["observations"]] == [
            expected.observation_id
        ]
        with pytest.raises(ValueError, match="invalid pagination cursor"):
            store.report_page(cursor="not-a-cursor", limit=5)
    finally:
        store.close()


def test_v1_report_cursor_pagination_is_opt_in_and_preserves_default_shape(
    tmp_path,
) -> None:
    store = IntelStore(tmp_path / "intel.json")
    observations = [_add(store, index) for index in range(1, 5)]
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        default_status, default_payload = _request_json(
            f"{server.url}/api/v1/reports?{urlencode({'limit': 2})}"
        )
        first_status, first = _request_json(
            f"{server.url}/api/v1/reports?"
            f"{urlencode({'cursor': 'start', 'limit': 2})}"
        )
        second_status, second = _request_json(
            f"{server.url}/api/v1/reports?"
            f"{urlencode({'cursor': first['next_cursor'], 'limit': 2})}"
        )

        assert default_status == first_status == second_status == 200
        assert set(default_payload) == {"reports", "count"}
        assert first["has_more"] is True
        assert second["has_more"] is False
        assert second["next_cursor"] is None
        assert [item["id"] for item in first["reports"] + second["reports"]] == [
            item.observation_id for item in reversed(observations)
        ]
    finally:
        server.stop()
        store.close()


def test_v1_observation_cursor_filters_and_returns_400_for_invalid_cursor(
    tmp_path,
) -> None:
    store = IntelStore(tmp_path / "intel.json")
    _add(store, 1, source="manual")
    expected = _add(store, 2, source="intel_channel")
    server = IntelHTTPServer(store, port=0)
    server.start()
    try:
        status, payload = _request_json(
            f"{server.url}/api/v1/observations?"
            f"{urlencode({'cursor': 'start', 'limit': 5, 'source': 'intel_channel'})}"
        )
        invalid_status, invalid = _request_json(
            f"{server.url}/api/v1/observations?cursor=invalid"
        )

        assert status == 200
        assert [item["id"] for item in payload["observations"]] == [
            expected.observation_id
        ]
        assert payload["has_more"] is False
        assert invalid_status == 400
        assert invalid == {"error": "invalid pagination cursor"}
    finally:
        server.stop()
        store.close()
