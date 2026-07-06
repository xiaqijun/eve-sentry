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

    API_V1_PREFIX = "/api/v1"

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
        return self._request("POST", self._v1_path("/reports"), payload=payload)

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
        received_at: str | None = None,
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
        if received_at is not None:
            payload["received_at"] = received_at
        return self._request("POST", self._v1_path("/observations"), payload=payload)

    def post_channel_line(self, line: str, channel: str = "") -> dict[str, Any]:
        """Publish one raw intel channel log line for server-side parsing."""
        payload = {"line": line}
        if channel:
            payload["channel"] = channel
        return self._request("POST", self._v1_path("/channel-lines"), payload=payload)

    def post_ocr_snapshot(
        self,
        client_id: str,
        source_instance: str,
        system_name: str,
        names: list[str],
        seen_at: str = "",
        system_id: int | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Publish the current OCR-detected pilot-name snapshot."""
        payload: dict[str, Any] = {
            "client_id": client_id,
            "source_instance": source_instance,
            "system_name": system_name,
            "names": names,
        }
        if seen_at:
            payload["seen_at"] = seen_at
        if system_id is not None:
            payload["system_id"] = system_id
        if confidence is not None:
            payload["confidence"] = confidence
        return self._request("POST", self._v1_path("/ocr/snapshot"), payload=payload)

    def get_active_intel(self, **params: Any) -> dict[str, Any]:
        """Fetch realtime active intel rows from the server."""
        return self._request("GET", self._v1_path("/active-intel"), params=params)

    def post_heartbeat(
        self,
        client_id: str,
        client_type: str,
        label: str = "",
        status: str = "running",
        heartbeat_interval_seconds: float = 0.0,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish one runtime heartbeat for client status visibility."""
        payload: dict[str, Any] = {
            "client_id": str(client_id or "").strip(),
            "client_type": str(client_type or "").strip(),
            "status": str(status or "").strip() or "running",
            "heartbeat_interval_seconds": float(heartbeat_interval_seconds),
        }
        if label:
            payload["label"] = label
        if details is not None:
            payload["details"] = details
        response = self._request(
            "POST",
            self._v1_path("/clients/heartbeats"),
            payload=payload,
        )
        heartbeat = response.get("heartbeat")
        if not isinstance(heartbeat, dict):
            raise IntelApiError("server returned an invalid heartbeat payload")
        return heartbeat

    def list_heartbeats(self) -> list[dict[str, Any]]:
        """Fetch recent runtime heartbeats from the intel server."""
        payload = self._request("GET", self._v1_path("/clients"))
        clients = payload.get("clients", payload)
        heartbeats = clients.get("heartbeats", []) if isinstance(clients, dict) else []
        if not isinstance(heartbeats, list):
            raise IntelApiError("server returned an invalid heartbeats payload")
        return heartbeats

    def client_status(self) -> dict[str, Any]:
        """Fetch recent runtime heartbeats plus aggregate summary."""
        payload = self._request("GET", self._v1_path("/clients"))
        clients = payload.get("clients")
        if not isinstance(clients, dict):
            raise IntelApiError("server returned an invalid clients payload")
        return clients

    def esi_status(self) -> dict[str, Any]:
        """Return authenticated ESI session status without token secrets."""
        return self._request("GET", self._v1_path("/esi/status"))

    def esi_session(
        self,
        include_location: bool = True,
        include_contacts: bool = True,
    ) -> dict[str, Any]:
        """Return the authenticated ESI session snapshot."""
        payload = self._request(
            "GET",
            self._v1_path("/esi/session"),
            params={
                "location": _bool_param(include_location),
                "contacts": _bool_param(include_contacts),
            },
        )
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise IntelApiError("server returned an invalid ESI session payload")
        return snapshot

    def system_profile(self, system_id: int) -> dict[str, Any]:
        """Fetch one solar-system profile by ESI id."""
        system_id = int(system_id)
        if system_id <= 0:
            raise IntelApiError("system_id must be a positive integer")
        payload = self._request("GET", self._v1_path(f"/systems/{system_id}"))
        system = payload.get("system")
        if not isinstance(system, dict):
            raise IntelApiError("server returned an invalid system payload")
        return system

    def character_profile(self, character_id: int) -> dict[str, Any]:
        """Fetch one character profile by ESI id."""
        character_id = int(character_id)
        if character_id <= 0:
            raise IntelApiError("character_id must be a positive integer")
        payload = self._request("GET", self._v1_path(f"/characters/{character_id}"))
        character = payload.get("character")
        if not isinstance(character, dict):
            raise IntelApiError("server returned an invalid character payload")
        return character

    def character_by_name(self, name: str) -> dict[str, Any]:
        """Resolve and fetch one character profile by exact name."""
        text = str(name or "").strip()
        if not text:
            raise IntelApiError("character name is required")
        payload = self._request(
            "GET",
            self._v1_path(f"/characters/by-name/{quote(text, safe='')}"),
        )
        character = payload.get("character")
        if not isinstance(character, dict):
            raise IntelApiError("server returned an invalid character payload")
        return character

    def system_by_name(self, name: str) -> dict[str, Any]:
        """Resolve and fetch one solar-system profile by exact name."""
        text = str(name or "").strip()
        if not text:
            raise IntelApiError("system name is required")
        payload = self._request(
            "GET",
            self._v1_path(f"/systems/by-name/{quote(text, safe='')}"),
        )
        system = payload.get("system")
        if not isinstance(system, dict):
            raise IntelApiError("server returned an invalid system payload")
        return system

    def character_kill_activity(self, character_id: int) -> dict[str, Any]:
        """Fetch recent killboard activity for one character."""
        return self._kill_activity("character", character_id, "character_id")

    def system_kill_activity(self, system_id: int) -> dict[str, Any]:
        """Fetch recent killboard activity for one solar system."""
        return self._kill_activity("system", system_id, "system_id")

    def corporation_kill_activity(self, corporation_id: int) -> dict[str, Any]:
        """Fetch recent killboard activity for one corporation."""
        return self._kill_activity("corporation", corporation_id, "corporation_id")

    def alliance_kill_activity(self, alliance_id: int) -> dict[str, Any]:
        """Fetch recent killboard activity for one alliance."""
        return self._kill_activity("alliance", alliance_id, "alliance_id")

    def _kill_activity(
        self,
        scope: str,
        entity_id: int,
        label: str,
    ) -> dict[str, Any]:
        entity_id = int(entity_id)
        if entity_id <= 0:
            raise IntelApiError(f"{label} must be a positive integer")
        payload = self._request(
            "GET",
            self._v1_path(f"/kill-activity/{scope}/{entity_id}"),
        )
        activity = payload.get("activity")
        if not isinstance(activity, dict):
            raise IntelApiError("server returned an invalid kill activity payload")
        return activity

    def bootstrap(self) -> dict[str, Any]:
        """Fetch the aggregated workbench bootstrap payload."""
        payload = self._request("GET", self._v1_path("/bootstrap"))
        bootstrap = payload.get("bootstrap")
        if not isinstance(bootstrap, dict):
            raise IntelApiError("server returned an invalid bootstrap payload")
        return bootstrap

    def map_snapshot(self) -> dict[str, Any]:
        """Fetch the current star-map snapshot for the workbench."""
        payload = self._request("GET", self._v1_path("/map"))
        snapshot = payload.get("map")
        if not isinstance(snapshot, dict):
            raise IntelApiError("server returned an invalid map payload")
        return snapshot

    def map_system(self, system_id: int) -> dict[str, Any]:
        """Fetch one system with both profile and related intel context."""
        system_id = int(system_id)
        if system_id <= 0:
            raise IntelApiError("system_id must be a positive integer")
        payload = self._request("GET", self._v1_path(f"/map/systems/{system_id}"))
        system = payload.get("system")
        if not isinstance(system, dict):
            raise IntelApiError("server returned an invalid map system payload")
        return system

    def current_esi_system(self) -> dict[str, Any] | None:
        """Return the current ESI solar system when the server session exposes it."""
        snapshot = self.esi_session(include_location=True, include_contacts=False)
        location = snapshot.get("location")
        if not isinstance(location, dict):
            return None

        system_id = _optional_positive_int(location.get("solar_system_id"))
        if system_id is None:
            return None

        embedded = location.get("solar_system")
        system = dict(embedded) if isinstance(embedded, dict) else {}
        name = str(
            location.get("solar_system_name")
            or system.get("name")
            or ""
        ).strip()
        if not name:
            try:
                system = self.system_profile(system_id)
                name = str(system.get("name") or "").strip()
            except IntelApiError:
                system = {}

        system["system_id"] = system_id
        if name:
            system["name"] = name
            system["system_name"] = name
        return system

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
        payload = self._request("GET", self._v1_path("/reports"), params=params)
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
        payload = self._request("GET", self._v1_path("/observations"), params=params)
        observations = payload.get("observations", [])
        if not isinstance(observations, list):
            raise IntelApiError("server returned an invalid observations payload")
        return observations

    def list_alerts(
        self,
        since: str = "",
        limit: int = 50,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch recent threat events from the intel server."""
        params = {"limit": str(limit)}
        if since:
            params["since"] = since
        if acknowledged is not None:
            params["acknowledged"] = "true" if acknowledged else "false"
        if min_score is not None:
            params["min_score"] = str(min_score)
        if min_level:
            params["min_level"] = min_level
        payload = self._request("GET", self._v1_path("/alerts"), params=params)
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
            self._v1_path(f"/alerts/{quote(alert_id, safe='')}"),
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
            self._v1_path(f"/alerts/{quote(alert_id, safe='')}/ack"),
            payload=payload,
        )
        alert = response.get("alert")
        if not isinstance(alert, dict):
            raise IntelApiError("server returned an invalid alert payload")
        return alert

    def stream_alerts(
        self,
        since: str = "",
        last_event_id: str = "",
        limit: int = 50,
        timeout: float = 30.0,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch alert events from the server-sent event stream."""
        return list(
            self.iter_alert_events(
                since=since,
                last_event_id=last_event_id,
                limit=limit,
                timeout=timeout,
                acknowledged=acknowledged,
                min_score=min_score,
                min_level=min_level,
            )
        )

    def iter_alert_events(
        self,
        since: str = "",
        last_event_id: str = "",
        limit: int = 50,
        timeout: float = 30.0,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str = "",
    ):
        """Yield alert events incrementally from the server-sent event stream."""
        params = {"limit": str(limit), "timeout": str(timeout)}
        if since:
            params["since"] = since
        if acknowledged is not None:
            params["acknowledged"] = "true" if acknowledged else "false"
        if min_score is not None:
            params["min_score"] = str(min_score)
        if min_level:
            params["min_level"] = min_level
        url = f"{self.base_url}{self._v1_path('/events')}?{urlencode(params)}"
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        request = Request(
            url,
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout + max(0.0, timeout)) as response:
                yield from self._iter_alert_events(response)
        except HTTPError as exc:
            message = self._read_error_message(exc)
            raise IntelApiError(message) from exc
        except URLError as exc:
            raise IntelApiError(str(exc.reason)) from exc
        except OSError as exc:
            raise IntelApiError(str(exc)) from exc

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

    def _v1_path(self, suffix: str) -> str:
        return f"{self.API_V1_PREFIX}{suffix}"

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
            alert = self._parse_alert_event_block(block.splitlines())
            if alert is None:
                continue
            alerts.append(alert)
        return alerts

    def _iter_alert_events(self, response):
        block_lines: list[str] = []
        while True:
            raw_line = response.readline()
            if raw_line == b"" or raw_line == "":
                if block_lines:
                    alert = self._parse_alert_event_block(block_lines)
                    if alert is not None:
                        yield alert
                return
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8")
            else:
                line = str(raw_line)
            line = line.rstrip("\r\n")
            if line == "":
                alert = self._parse_alert_event_block(block_lines)
                block_lines = []
                if alert is not None:
                    yield alert
                continue
            block_lines.append(line)

    def _parse_alert_event_block(self, lines: list[str]) -> dict[str, Any] | None:
        event_name = ""
        data_lines = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if event_name not in {"", "alert"} or not data_lines:
            return None
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise IntelApiError("server returned invalid SSE JSON") from exc
        if not isinstance(payload, dict):
            raise IntelApiError("server returned non-object SSE event data")
        return payload


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


def _optional_positive_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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

    def __init__(
        self,
        api: IntelApiClient,
        limit: int = 50,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str = "",
        seen_ids: list[str] | None = None,
    ) -> None:
        self.api = api
        self.limit = limit
        self.acknowledged = acknowledged
        self.min_score = min_score
        self.min_level = min_level
        self._seen_ids: set[str] = {
            str(alert_id).strip()
            for alert_id in seen_ids or []
            if str(alert_id).strip()
        }
        self._stream_since = ""
        self._stream_last_event_id = ""

    def seed_existing(self) -> list[dict[str, Any]]:
        """Mark currently known alerts as already seen."""
        seeded = []
        for alert in self.api.list_alerts(
            limit=self.limit,
            acknowledged=self.acknowledged,
            min_score=self.min_score,
            min_level=self.min_level,
        ):
            alert_id = str(alert.get("id") or "")
            if alert_id:
                self._seen_ids.add(alert_id)
                seeded.append(alert)
        self._remember_alert_cursor(seeded)
        return seeded

    def poll_new(self) -> list[dict[str, Any]]:
        """Return alerts that were not returned by previous polls."""
        alerts = self.api.list_alerts(
            limit=self.limit,
            acknowledged=self.acknowledged,
            min_score=self.min_score,
            min_level=self.min_level,
        )
        self._remember_alert_cursor(alerts)
        return self._filter_new(alerts, newest_first=True)

    def stream_new(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Return new alerts from the server-sent event stream."""
        since = "" if self._stream_last_event_id else self._stream_since
        alerts = self.api.stream_alerts(
            since=since,
            last_event_id=self._stream_last_event_id,
            limit=self.limit,
            timeout=timeout,
            acknowledged=self.acknowledged,
            min_score=self.min_score,
            min_level=self.min_level,
        )
        self._remember_alert_cursor(alerts)
        return self._filter_new(alerts, newest_first=False)

    def iter_stream_new(self, timeout: float = 30.0):
        """Yield new alerts from the server-sent event stream as they arrive."""
        since = "" if self._stream_last_event_id else self._stream_since
        for alert in self.api.iter_alert_events(
            since=since,
            last_event_id=self._stream_last_event_id,
            limit=self.limit,
            timeout=timeout,
            acknowledged=self.acknowledged,
            min_score=self.min_score,
            min_level=self.min_level,
        ):
            self._remember_alert_cursor([alert])
            new_alerts = self._filter_new([alert], newest_first=False)
            if new_alerts:
                yield new_alerts[0]

    def _remember_alert_cursor(self, alerts: list[dict[str, Any]]) -> None:
        for alert in alerts:
            cursor = str(alert.get("created_at") or alert.get("seen_at") or "")
            alert_id = str(alert.get("id") or "")
            if cursor and cursor > self._stream_since:
                self._stream_since = cursor
                self._stream_last_event_id = alert_id
            elif cursor and cursor == self._stream_since and alert_id:
                self._stream_last_event_id = alert_id

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
