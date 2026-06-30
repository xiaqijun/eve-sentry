"""HTTP client helpers for publishing and consuming intel reports."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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

    def post_observation(
        self,
        system_name: str,
        names: list[str] | None = None,
        source: str = "api",
        source_instance: str = "",
        system_id: int | None = None,
        character_ids: list[int] | None = None,
        confidence: float | None = None,
        raw_text: str = "",
        metadata: dict[str, Any] | None = None,
        seen_at: str | None = None,
    ) -> dict[str, Any]:
        """Publish a canonical multi-source observation."""
        payload: dict[str, Any] = {
            "system_name": system_name,
            "names": names or [],
            "source": source,
            "source_instance": source_instance,
            "character_ids": character_ids or [],
            "raw_text": raw_text,
        }
        if system_id is not None:
            payload["system_id"] = system_id
        if confidence is not None:
            payload["confidence"] = confidence
        if metadata is not None:
            payload["metadata"] = metadata
        if seen_at is not None:
            payload["seen_at"] = seen_at
        return self._request("POST", "/api/observations", payload=payload)

    def post_channel_line(self, line: str, channel: str = "") -> dict[str, Any]:
        """Publish one raw intel channel log line for server-side parsing."""
        payload = {"line": line}
        if channel:
            payload["channel"] = channel
        return self._request("POST", "/api/channel-lines", payload=payload)

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

    def list_observations(
        self,
        source: str = "",
        system: str = "",
        name: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch recent observations from the intel server."""
        params = {"limit": str(limit)}
        if source:
            params["source"] = source
        if system:
            params["system"] = system
        if name:
            params["name"] = name
        payload = self._request("GET", "/api/observations", params=params)
        observations = payload.get("observations", [])
        if not isinstance(observations, list):
            raise IntelApiError("server returned an invalid observations payload")
        return observations

    def list_alerts(
        self,
        since: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch recent threat events from the intel server."""
        params = {"limit": str(limit)}
        if since:
            params["since"] = since
        payload = self._request("GET", "/api/alerts", params=params)
        alerts = payload.get("alerts", [])
        if not isinstance(alerts, list):
            raise IntelApiError("server returned an invalid alerts payload")
        return alerts

    def alert_detail(self, alert_id: str) -> dict[str, Any]:
        """Fetch one alert with source observation and explanation context."""
        alert_id = str(alert_id or "").strip()
        if not alert_id:
            raise IntelApiError("alert_id is required")
        payload = self._request(
            "GET",
            f"/api/alerts/{quote(alert_id, safe='')}",
        )
        detail = payload.get("detail")
        if not isinstance(detail, dict):
            raise IntelApiError("server returned an invalid alert detail payload")
        return detail

    def ack_alert(
        self,
        alert_id: str,
        acknowledged_by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Mark one alert as acknowledged on the intel server."""
        alert_id = str(alert_id or "").strip()
        if not alert_id:
            raise IntelApiError("alert_id is required")

        payload: dict[str, Any] = {}
        if acknowledged_by:
            payload["acknowledged_by"] = acknowledged_by
        if note:
            payload["note"] = note
        response = self._request(
            "POST",
            f"/api/alerts/{quote(alert_id, safe='')}/ack",
            payload=payload,
        )
        alert = response.get("alert")
        if not isinstance(alert, dict):
            raise IntelApiError("server returned an invalid alert payload")
        return alert

    def stream_alerts(
        self,
        since: str = "",
        limit: int = 50,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Fetch alert events from the server-sent event stream."""
        params = {"limit": str(limit), "timeout": str(timeout)}
        if since:
            params["since"] = since
        url = f"{self.base_url}/api/events?{urlencode(params)}"
        request = Request(
            url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout + max(0.0, timeout)) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            message = self._read_error_message(exc)
            raise IntelApiError(message) from exc
        except URLError as exc:
            raise IntelApiError(str(exc.reason)) from exc
        except OSError as exc:
            raise IntelApiError(str(exc)) from exc
        return self._parse_alert_events(body)

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

    def _parse_alert_events(self, body: str) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for block in body.split("\n\n"):
            event_name = ""
            data_lines = []
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
            if event_name not in {"", "alert"} or not data_lines:
                continue
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError as exc:
                raise IntelApiError("server returned invalid SSE JSON") from exc
            if not isinstance(payload, dict):
                raise IntelApiError("server returned non-object SSE event data")
            alerts.append(payload)
        return alerts


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


class AlertPoller:
    """Track seen alert ids and return only newly generated threat events."""

    def __init__(self, api: IntelApiClient, limit: int = 50) -> None:
        self.api = api
        self.limit = limit
        self._seen_ids: set[str] = set()

    def seed_existing(self) -> None:
        """Mark currently known alerts as already seen."""
        for alert in self.api.list_alerts(limit=self.limit):
            alert_id = str(alert.get("id") or "")
            if alert_id:
                self._seen_ids.add(alert_id)

    def poll_new(self) -> list[dict[str, Any]]:
        """Return alerts that were not returned by previous polls."""
        alerts = self.api.list_alerts(limit=self.limit)
        return self._filter_new(alerts, newest_first=True)

    def stream_new(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Return new alerts from the server-sent event stream."""
        alerts = self.api.stream_alerts(limit=self.limit, timeout=timeout)
        return self._filter_new(alerts, newest_first=False)

    def _filter_new(
        self,
        alerts: list[dict[str, Any]],
        newest_first: bool,
    ) -> list[dict[str, Any]]:
        new_alerts: list[dict[str, Any]] = []
        ordered_alerts = reversed(alerts) if newest_first else alerts
        for alert in ordered_alerts:
            alert_id = str(alert.get("id") or "")
            if not alert_id or alert_id in self._seen_ids:
                continue
            self._seen_ids.add(alert_id)
            new_alerts.append(alert)
        return new_alerts
