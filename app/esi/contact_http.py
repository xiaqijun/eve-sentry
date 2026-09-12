"""Bounded private contact pagination and HTTP freshness, without token logging."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.request import Request

LOG = logging.getLogger(__name__)


class ContactReadError(RuntimeError):
    """Safe structured error; never includes an upstream body or bearer token."""

    def __init__(self, reason, *, status=None, retry_after=0.0):
        super().__init__(reason)
        self.status = status
        self.retry_after = retry_after


class ContactRows(list):
    """List-compatible complete source snapshot with its original deadline."""

    def __init__(self, rows, *, expires_at, degraded=False):
        super().__init__(rows)
        self.expires_at = expires_at
        self.degraded = degraded


def http_date(value):
    try:
        result = parsedate_to_datetime(value).timestamp()
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None


def freshness(headers, *, started, received):
    """Conservative RFC-style corrected age; never renew an aged response by 300s."""
    headers = {key.lower(): value for key, value in headers.items()}
    date = http_date(headers.get("date"))
    expires = http_date(headers.get("expires"))
    directives = {}
    for part in headers.get("cache-control", "").lower().split(","):
        key, _, value = part.strip().partition("=")
        directives[key] = value.strip('"')
    try:
        age = float(headers.get("age", 0))
        if not math.isfinite(age) or age < 0:
            raise ValueError()
        lifetimes = []
        if "max-age" in directives:
            lifetime = float(directives["max-age"])
            if not math.isfinite(lifetime) or lifetime < 0:
                raise ValueError()
            lifetimes.append(lifetime)
        if expires is not None and date is not None:
            lifetimes.append(max(0.0, expires - date))
        if "no-cache" in directives or "no-store" in directives:
            return received, False
        if date is None or not lifetimes:
            return received, True
        corrected_age = max(max(0.0, received - date), age + max(0.0, received - started))
        return received + max(0.0, min(300.0, *lifetimes) - corrected_age), False
    except (ValueError, TypeError):
        return received, True


def validate_rows(rows):
    if not isinstance(rows, list):
        raise ContactReadError("invalid_contacts_payload")
    seen = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContactReadError("invalid_contact_row")
        cid, kind, standing = row.get("contact_id"), row.get("contact_type"), row.get("standing")
        if (type(cid) is not int or cid <= 0 or not isinstance(kind, str)
                or kind not in {"character", "corporation", "alliance", "faction"}
                or type(standing) not in (int, float) or not math.isfinite(standing) or not -10 <= standing <= 10):
            raise ContactReadError("invalid_contact_row")
        key = kind, cid
        if key in seen:
            raise ContactReadError("duplicate_contact_across_pages")
        seen[key] = standing
    return rows


def failure_details(exc, now):
    """Inspect typed causes, not potentially sensitive exception strings."""
    status, delay = None, 0.0
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = getattr(exc, "status", None) or getattr(exc, "code", None) or status
        delay = max(delay, getattr(exc, "retry_after", 0.0) or 0.0)
        headers = getattr(exc, "headers", None)
        if headers:
            value = headers.get("Retry-After")
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = max(0.0, (http_date(value) or now) - now)
            if math.isfinite(parsed):
                delay = max(delay, parsed)
        exc = exc.__cause__
    return status, delay


class ContactHttpReader:
    """Commit validator bodies only after all pages succeed; serialize private reads."""

    def __init__(self, *, now=time.time, monotonic=time.monotonic):
        self.now, self.monotonic = now, monotonic
        self._lock = threading.Lock()
        self._pages = OrderedDict()
        self._metrics = {"requests": 0, "attempts": 0, "success": 0, "errors": 0, "not_modified": 0}

    def metrics(self):
        with self._lock:
            return dict(self._metrics)

    def read(self, url, token, *, opener, timeout, user_agent):
        with self._lock:
            self._metrics["requests"] += 1
            began = self.monotonic()
            source = url.rstrip("/").split("/")[-3]
            page = 1
            try:
                context = hashlib.sha256(token.encode()).digest(), url
                previous = self._pages.get(context, {})
                staged, rows, deadlines = {}, [], []
                count, degraded, total_bytes = 1, False, 0
                while page <= count:
                    remaining = 30.0 - (self.monotonic() - began)
                    if remaining <= 0:
                        raise ContactReadError("contacts_deadline")
                    headers = {"Accept": "application/json", "User-Agent": user_agent,
                               "Authorization": f"Bearer {token}"}
                    cached = previous.get(page)
                    if cached and cached[1].get("etag"):
                        headers["If-None-Match"] = cached[1]["etag"]
                    request = Request(url if page == 1 else f"{url}?page={page}", headers=headers)
                    self._metrics["attempts"] += 1
                    started = self.now()
                    try:
                        response = opener(request, timeout=min(timeout, remaining))
                    except HTTPError as exc:
                        if exc.code != 304:
                            status, retry = failure_details(exc, self.now())
                            exc.close()
                            raise ContactReadError("contacts_http_error", status=status, retry_after=retry) from exc
                        response = exc
                    with response:
                        metadata = {key.lower(): value for key, value in getattr(response, "headers", {}).items()}
                        if getattr(response, "code", None) == 304 or getattr(response, "status", None) == 304:
                            if cached is None:
                                raise ContactReadError("contacts_304_without_body")
                            # Never inherit old Date/Expires/Age on a malformed 304.
                            body = cached[0]
                            total_bytes += len(json.dumps(body).encode("utf-8"))
                            metadata.setdefault("x-pages", cached[1].get("x-pages", "1"))
                            metadata.setdefault("etag", cached[1].get("etag", ""))
                            self._metrics["not_modified"] += 1
                        else:
                            chunks, size = [], 0
                            read_chunk = getattr(response, "read1", response.read)
                            while True:
                                if self.monotonic() - began >= 30:
                                    raise ContactReadError("contacts_deadline")
                                chunk = read_chunk(min(65536, 4 * 1024 * 1024 + 1 - size))
                                if not chunk:
                                    break
                                chunks.append(chunk)
                                size += len(chunk)
                                if size > 4 * 1024 * 1024:
                                    raise ContactReadError("contacts_body_limit")
                            raw = b"".join(chunks)
                            total_bytes += len(raw)
                            body = json.loads(raw)
                    if total_bytes > 8 * 1024 * 1024:
                        raise ContactReadError("contacts_snapshot_limit")
                    received = self.now()
                    if self.monotonic() - began >= 30:
                        raise ContactReadError("contacts_deadline")
                    declared = int(metadata.get("x-pages", "1"))
                    if not 1 <= declared <= 100 or (page > 1 and declared != count):
                        raise ContactReadError("contacts_pages_changed")
                    count = declared
                    validate_rows(body)
                    rows.extend(body)
                    if len(rows) > 20000:
                        raise ContactReadError("contacts_row_limit")
                    deadline, bad_headers = freshness(metadata, started=started, received=received)
                    deadlines.append(deadline)
                    degraded = degraded or bad_headers
                    staged[page] = body, metadata
                    page += 1
                validate_rows(rows)
                # Pages which expired while later pages loaded are not a coherent usable snapshot.
                if count > 1 and (degraded or min(deadlines) <= self.now()):
                    raise ContactReadError("contacts_pages_expired")
                if not any("no-store" in meta.get("cache-control", "").lower() for _, meta in staged.values()):
                    self._pages[context] = staged
                    self._pages.move_to_end(context)
                    while len(self._pages) > 6:
                        self._pages.popitem(last=False)
                else:
                    self._pages.pop(context, None)
                self._metrics["success"] += 1
                LOG.info("esi_contacts source=%s pages=%s rows=%s elapsed_ms=%.1f degraded=%s",
                         source, count, len(rows), (self.monotonic() - began) * 1000, degraded)
                return ContactRows(copy.deepcopy(rows), expires_at=min(deadlines), degraded=degraded)
            except Exception as exc:
                self._metrics["errors"] += 1
                status, retry = failure_details(exc, self.now())
                code = str(exc) if isinstance(exc, ContactReadError) else type(exc).__name__
                LOG.warning("esi_contacts source=%s page=%s status=%s error=%s elapsed_ms=%.1f retry_after=%.1f",
                            source, page, status, code, (self.monotonic() - began) * 1000, retry)
                if isinstance(exc, ContactReadError):
                    raise
                raise ContactReadError("contacts_read_failed", status=status, retry_after=retry) from exc
