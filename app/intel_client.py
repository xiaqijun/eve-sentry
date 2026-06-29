"""HTTP client helpers for publishing and consuming intel reports."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class IntelApiError(RuntimeError):
    """Raised when the intel server cannot satisfy an API request."""


class IntelApiClient:
    """Small JSON client for the EVE Sentry intel HTTP API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post_report(
        self,
        system: str,
        names: list[str],
        source: str = "detector",
        confidence: float | None = None,
        note: str = "",
        seen_at: str | None = None,
    ) -> dict[str, Any]:
        """Publish a hostile sighting report."""
        payload: dict[str, Any] = {
            "system": system,
            "names": names,
            "source": source,
            "note": note,
        }
        if confidence is not None:
            payload["confidence"] = confidence
        if seen_at is not None:
            payload["seen_at"] = seen_at
        return self._request("POST", "/api/intel", payload=payload)

    def list_reports(
        self,
        system: str = "",
        name: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch recent reports from the intel server."""
        params = {"limit": str(limit)}
        if system:
            params["system"] = system
        if name:
            params["name"] = name
        payload = self._request("GET", "/api/reports", params=params)
        reports = payload.get("reports", [])
        if not isinstance(reports, list):
            raise IntelApiError("server returned an invalid reports payload")
        return reports

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            message = self._read_error_message(exc)
            raise IntelApiError(message) from exc
        except URLError as exc:
            raise IntelApiError(str(exc.reason)) from exc
        except OSError as exc:
            raise IntelApiError(str(exc)) from exc

        if not body:
            return {}
        try:
            data_obj = json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntelApiError("server returned invalid JSON") from exc
        if not isinstance(data_obj, dict):
            raise IntelApiError("server returned a non-object JSON payload")
        return data_obj

    def _read_error_message(self, exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(payload, dict) and payload.get("error"):
                return str(payload["error"])
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        return f"HTTP {exc.code}"


class ReportPoller:
    """Track seen report ids and return only newly observed reports."""

    def __init__(self, api: IntelApiClient, limit: int = 50) -> None:
        self.api = api
        self.limit = limit
        self._seen_ids: set[str] = set()

    def seed_existing(self) -> None:
        """Mark currently known reports as already seen."""
        for report in self.api.list_reports(limit=self.limit):
            report_id = str(report.get("id") or "")
            if report_id:
                self._seen_ids.add(report_id)

    def poll_new(self) -> list[dict[str, Any]]:
        """Return reports that were not returned by previous polls."""
        reports = self.api.list_reports(limit=self.limit)
        new_reports: list[dict[str, Any]] = []
        for report in reversed(reports):
            report_id = str(report.get("id") or "")
            if not report_id or report_id in self._seen_ids:
                continue
            self._seen_ids.add(report_id)
            new_reports.append(report)
        return new_reports
