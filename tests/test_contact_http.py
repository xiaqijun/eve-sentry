"""Deterministic private ESI HTTP tests; no live network or credentials."""

import io
import json
from email.utils import formatdate
from urllib.error import HTTPError

import pytest

from app.esi.contact_http import (
    ContactHttpReader,
    ContactReadError,
    freshness,
    validate_rows,
)


def headers(**extra):
    return {"Date": formatdate(1000, usegmt=True), "Expires": formatdate(1300, usegmt=True),
            "X-Pages": "1", "ETag": '"version-one"', **extra}


def row(cid=1, standing=5):
    return {"contact_id": cid, "contact_type": "corporation", "standing": standing}


class Response(io.BytesIO):
    def __init__(self, body, metadata=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers() if metadata is None else metadata
        self.status = 200


def read(reader, opener, token="private-test-token"):
    return reader.read("https://esi.test/latest/alliances/10/contacts/", token,
                       opener=opener, timeout=10, user_agent="tests")


def test_response_age_and_transit_are_not_fresh_ttl():
    deadline, degraded = freshness(headers(Age="200"), started=1000, received=1002)
    assert deadline == 1100
    assert not degraded
    deadline, _ = freshness(headers(), started=1100, received=1120)
    assert deadline == 1300


@pytest.mark.parametrize("metadata", [{}, {"Date": "invalid"}, headers(Age="nan"), headers(Age="-1"),
                                      headers(**{"Cache-Control": "max-age=nan"})])
def test_missing_or_invalid_freshness_is_not_renewed(metadata):
    assert freshness(metadata, started=1000, received=1001) == (1001, True)


def test_conflicting_deadlines_choose_earliest_and_respect_no_cache():
    assert freshness(headers(**{"Cache-Control": "max-age=60"}), started=1000, received=1001)[0] == 1060
    assert freshness(headers(**{"Cache-Control": "no-cache"}), started=1000, received=1001)[0] == 1001


def test_complete_pagination_preserves_explicit_zero():
    reader = ContactHttpReader(now=lambda: 1001)
    calls = []
    def opener(request, timeout):
        calls.append(request.full_url)
        return Response([row(len(calls), 0)], headers(**{"X-Pages": "2"}))
    result = read(reader, opener)
    assert result == [row(1, 0), row(2, 0)]
    assert calls[1].endswith("?page=2")
    assert result.expires_at == 1300
    assert reader.metrics() == {"requests": 1, "attempts": 2, "success": 1, "errors": 0, "not_modified": 0}


def test_partial_failure_cannot_publish_staged_validator_bodies():
    reader = ContactHttpReader(now=lambda: 1001)
    old = read(reader, lambda *_args, **_kw: Response([row()]))
    attempts = []
    def fails(request, timeout):
        attempts.append(request)
        if len(attempts) == 2:
            raise TimeoutError("secret error text")
        return Response([row(2)], headers(**{"X-Pages": "2", "ETag": '"new"'}))
    with pytest.raises(ContactReadError):
        read(reader, fails)
    def not_modified(request, timeout):
        assert request.get_header("If-none-match") == '"version-one"'
        raise HTTPError(request.full_url, 304, "", headers(), io.BytesIO())
    assert read(reader, not_modified) == old


@pytest.mark.parametrize("second", [Response([row(2)], headers(**{"X-Pages": "3"})),
                                  Response([row()], headers(**{"X-Pages": "2"}))])
def test_page_count_changes_and_duplicate_rows_are_rejected(second):
    reader = ContactHttpReader(now=lambda: 1001)
    pages = iter([Response([row()], headers(**{"X-Pages": "2"})), second])
    with pytest.raises(ContactReadError):
        read(reader, lambda *_args, **_kw: next(pages))
    assert not reader._pages


def test_304_requires_same_authority_body_and_renews_from_new_headers():
    clock = [1001]
    reader = ContactHttpReader(now=lambda: clock[0])
    original = read(reader, lambda *_args, **_kw: Response([row()]))
    original[0]["standing"] = -10  # Caller mutations must not corrupt validator bodies.
    clock[0] = 1301
    def not_modified(request, timeout):
        raise HTTPError(request.full_url, 304, "", headers(Date=formatdate(1300, usegmt=True),
                        Expires=formatdate(1600, usegmt=True)), io.BytesIO())
    result = read(reader, not_modified)
    assert result == [row()]
    assert result.expires_at == 1600
    with pytest.raises(ContactReadError, match="304_without_body"):
        read(reader, not_modified, token="different-authority")


def test_304_missing_freshness_does_not_inherit_old_lifetime():
    reader = ContactHttpReader(now=lambda: 1001)
    read(reader, lambda *_args, **_kw: Response([row()]))
    def not_modified(request, timeout):
        raise HTTPError(request.full_url, 304, "", {}, io.BytesIO())
    result = read(reader, not_modified)
    assert result.degraded
    assert result.expires_at == 1001


def test_rate_error_has_safe_details_and_no_automatic_retry(caplog):
    reader = ContactHttpReader(now=lambda: 1001)
    def throttle(request, timeout):
        raise HTTPError(request.full_url, 429, "private-test-token", {"Retry-After": "123"},
                        io.BytesIO(b"private response"))
    with pytest.raises(ContactReadError) as raised:
        read(reader, throttle)
    assert raised.value.status == 429
    assert raised.value.retry_after == 123
    assert reader.metrics()["attempts"] == 1
    assert "private-test-token" not in caplog.text
    assert "private response" not in caplog.text


@pytest.mark.parametrize("bad", [None, {}, [None], [row(0)], [row(True)], [row(1, float("nan"))],
                                 [row(1, 11)], [{**row(), "contact_type": []}], [row(), row()]])
def test_invalid_contacts_never_become_an_empty_success(bad):
    with pytest.raises(ContactReadError):
        validate_rows(bad)


def test_no_store_does_not_leave_private_validator_body():
    reader = ContactHttpReader(now=lambda: 1001)
    read(reader, lambda *_args, **_kw: Response([row()], headers(**{"Cache-Control": "no-store"})))
    assert not reader._pages


def test_deadline_and_oversized_page_are_rejected():
    clock = [0]
    reader = ContactHttpReader(now=lambda: 1001, monotonic=lambda: clock[0])
    def slow(*_args, **_kw):
        clock[0] = 31
        return Response([row()])
    with pytest.raises(ContactReadError, match="deadline"):
        read(reader, slow)
    reader = ContactHttpReader(now=lambda: 1001)
    with pytest.raises(ContactReadError, match="body_limit"):
        read(reader, lambda *_args, **_kw: Response("x" * (4 * 1024 * 1024)))
