"""Local HTTP server exposing the hostile intel map and JSON API."""

from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.channels.parser import parse_chat_line
from app.esi.sso import EsiSsoError
from app.server.intel_store import IntelStore
from app.server.star_map_page import INDEX_HTML

logger = logging.getLogger(__name__)


class IntelHTTPServer:
    """Small background HTTP server for local intel sharing."""

    def __init__(
        self,
        store: IntelStore,
        host: str = "127.0.0.1",
        port: int = 8765,
        config_store: Any | None = None,
        esi_session: Any | None = None,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.config_store = config_store
        self.esi_session = esi_session
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """Start the server in a daemon thread."""
        if self._thread and self._thread.is_alive():
            return

        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.store = self.store  # type: ignore[attr-defined]
        self._httpd.config_store = self.config_store  # type: ignore[attr-defined]
        self._httpd.esi_session = self.esi_session  # type: ignore[attr-defined]
        self.host, self.port = self._httpd.server_address[:2]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="eve-sentry-intel-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("Intel server listening on %s", self.url)

    def stop(self) -> None:
        """Stop the background server."""
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None

    def _make_handler(self):
        class Handler(IntelRequestHandler):
            pass

        return Handler


class IntelRequestHandler(BaseHTTPRequestHandler):
    """Request handler for the local intel service."""

    server_version = "EveSentryIntel/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path == "/api/config":
            config_store = self._config_store()
            if config_store is None:
                self._send_json({"error": "config not enabled"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"config": config_store.to_dict()})
            return
        if path == "/api/esi/status":
            self._send_json(self._esi_status_payload())
            return
        if path in {"/api/esi/session", "/api/esi/snapshot"}:
            query = parse_qs(parsed.query)
            try:
                include_location = self._parse_optional_bool_default(
                    query.get("location", [""])[0],
                    default=True,
                    label="location",
                )
                include_contacts = self._parse_optional_bool_default(
                    query.get("contacts", [""])[0],
                    default=True,
                    label="contacts",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_esi_snapshot(include_location, include_contacts)
            return
        if path in {"/api/intel", "/api/systems"}:
            snapshot = self._store().snapshot()
            if path == "/api/systems":
                snapshot = {
                    "systems": snapshot["systems"],
                    "links": snapshot["links"],
                    "generated_at": snapshot["generated_at"],
                }
            self._send_json(snapshot)
            return
        if path.startswith("/api/characters/by-name/"):
            name = unquote(path[len("/api/characters/by-name/"):]).strip()
            self._send_optional_json(
                "character",
                self._store().character_by_name(name),
                "character not found or ESI not enabled",
            )
            return
        if path.startswith("/api/characters/"):
            try:
                character_id = self._parse_path_int(
                    path,
                    "/api/characters/",
                    "character_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "character",
                self._store().character_profile(character_id),
                "character not found or ESI not enabled",
            )
            return
        if path.startswith("/api/systems/by-name/"):
            name = unquote(path[len("/api/systems/by-name/"):]).strip()
            self._send_optional_json(
                "system",
                self._store().system_by_name(name),
                "system not found or ESI not enabled",
            )
            return
        if path.startswith("/api/kill-activity/character/"):
            try:
                character_id = self._parse_path_int(
                    path,
                    "/api/kill-activity/character/",
                    "character_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "activity",
                self._store().character_kill_activity(character_id),
                "character kill activity not found or killboard not enabled",
            )
            return
        if path.startswith("/api/kill-activity/system/"):
            try:
                system_id = self._parse_path_int(
                    path,
                    "/api/kill-activity/system/",
                    "system_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "activity",
                self._store().system_kill_activity(system_id),
                "system kill activity not found or killboard not enabled",
            )
            return
        if path.startswith("/api/kill-activity/corporation/"):
            try:
                corporation_id = self._parse_path_int(
                    path,
                    "/api/kill-activity/corporation/",
                    "corporation_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "activity",
                self._store().corporation_kill_activity(corporation_id),
                "corporation kill activity not found or killboard not enabled",
            )
            return
        if path.startswith("/api/kill-activity/alliance/"):
            try:
                alliance_id = self._parse_path_int(
                    path,
                    "/api/kill-activity/alliance/",
                    "alliance_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "activity",
                self._store().alliance_kill_activity(alliance_id),
                "alliance kill activity not found or killboard not enabled",
            )
            return
        if path == "/api/reports":
            query = parse_qs(parsed.query)
            try:
                limit = self._parse_optional_int(query.get("limit", [""])[0])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            reports = self._store().list_reports(
                system=query.get("system", [""])[0],
                name=query.get("name", [""])[0],
                limit=limit,
            )
            self._send_json({"reports": reports, "count": len(reports)})
            return
        if path == "/api/observations":
            query = parse_qs(parsed.query)
            try:
                limit = self._parse_optional_int(query.get("limit", [""])[0])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            observations = self._store().list_observations(
                source=query.get("source", [""])[0],
                system=query.get("system", [""])[0],
                name=query.get("name", [""])[0],
                limit=limit,
            )
            self._send_json(
                {"observations": observations, "count": len(observations)}
            )
            return
        if path == "/api/alerts":
            query = parse_qs(parsed.query)
            try:
                limit = self._parse_optional_int(query.get("limit", [""])[0])
                filters = self._parse_alert_filters(query)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            alerts = self._store().list_alerts(
                since=query.get("since", [""])[0],
                limit=limit,
                **filters,
            )
            self._send_json({"alerts": alerts, "count": len(alerts)})
            return
        if path.startswith("/api/alerts/"):
            alert_id = unquote(path[len("/api/alerts/"):]).strip()
            detail = self._store().alert_detail(alert_id)
            if detail is None:
                self._send_json({"error": "alert not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"detail": detail})
            return
        if path == "/api/events":
            query = parse_qs(parsed.query)
            try:
                parsed_limit = self._parse_optional_int(query.get("limit", [""])[0])
                parsed_timeout = self._parse_optional_float(
                    query.get("timeout", [""])[0]
                )
                filters = self._parse_alert_filters(query)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._stream_events(
                since=query.get("since", [""])[0],
                limit=50 if parsed_limit is None else parsed_limit,
                timeout_seconds=30.0 if parsed_timeout is None else parsed_timeout,
                **filters,
            )
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        ack_prefix = "/api/alerts/"
        if path.startswith(ack_prefix) and path.endswith("/ack"):
            alert_id = unquote(path[len(ack_prefix):-len("/ack")]).strip()
            if not alert_id:
                self._send_json(
                    {"error": "alert id is required"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                payload = self._read_optional_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            alert = self._store().ack_alert(
                alert_id,
                acknowledged_by=str(
                    payload.get("acknowledged_by") or payload.get("by") or ""
                ),
                note=str(payload.get("note") or ""),
            )
            if alert is None:
                self._send_json({"error": "alert not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "alert": alert})
            return

        if path == "/api/channel-lines":
            try:
                result = self._add_channel_line(self._read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                result,
                HTTPStatus.OK if result.get("ignored") else HTTPStatus.CREATED,
            )
            return

        if path not in {"/api/intel", "/api/observations"}:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_json()
            if path == "/api/observations":
                observation = self._store().add_observation(payload)
                self._send_json(
                    {
                        "ok": True,
                        "observation": observation.to_dict(),
                        "alert": self._alert_for_observation(
                            observation.observation_id
                        ),
                    },
                    HTTPStatus.CREATED,
                )
                return

            report = self._store().add_report(
                system=str(payload.get("system", "")),
                names=payload.get("names", []),
                source=str(payload.get("source", "api")),
                confidence=payload.get("confidence"),
                note=str(payload.get("note", "")),
                seen_at=payload.get("seen_at"),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json(
            {
                "ok": True,
                "report": report.to_dict(),
                "observation": report.to_observation().to_dict(),
                "alert": self._alert_for_observation(report.report_id),
            },
            HTTPStatus.CREATED,
        )

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/config":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        config_store = self._config_store()
        if config_store is None:
            self._send_json({"error": "config not enabled"}, HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_json()
            config = config_store.update(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self._store().set_scorer(config.build_scorer())
        self._send_json({"ok": True, "config": config.to_dict()})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        prefix = "/api/intel/"
        if not path.startswith(prefix):
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        report_id = unquote(path[len(prefix):]).strip()
        if not self._store().delete_report(report_id):
            self._send_json({"error": "report not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True, "id": report_id})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers(content_type=None, content_length=0)
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, DELETE, OPTIONS",
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("intel-server: " + format, *args)

    def _store(self) -> IntelStore:
        return self.server.store  # type: ignore[attr-defined,no-any-return]

    def _config_store(self) -> Any | None:
        return self.server.config_store  # type: ignore[attr-defined,no-any-return]

    def _esi_session(self) -> Any | None:
        return self.server.esi_session  # type: ignore[attr-defined,no-any-return]

    def _esi_status_payload(self) -> dict[str, Any]:
        session = self._esi_session()
        if session is None:
            return {"enabled": False, "authenticated": False}
        if not hasattr(session, "load_tokens"):
            return {
                "enabled": True,
                "authenticated": False,
                "error": "ESI session cannot load tokens",
            }
        try:
            tokens = session.load_tokens(refresh_if_needed=False)
        except EsiSsoError as exc:
            return {"enabled": True, "authenticated": False, "error": str(exc)}
        return {
            "enabled": True,
            "authenticated": True,
            "character_id": tokens.character_id,
            "character_owner_hash": tokens.character_owner_hash,
            "scopes": list(tokens.scopes),
            "expires_at": tokens.expires_at,
            "expired": bool(tokens.is_expired()),
        }

    def _send_esi_snapshot(
        self,
        include_location: bool,
        include_contacts: bool,
    ) -> None:
        session = self._esi_session()
        if session is None or not hasattr(session, "snapshot"):
            self._send_json({"error": "ESI session not enabled"}, HTTPStatus.NOT_FOUND)
            return
        try:
            snapshot = session.snapshot(
                include_location=include_location,
                include_contacts=include_contacts,
            )
        except EsiSsoError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            return
        except Exception as exc:
            self._send_json(
                {"error": f"ESI session unavailable: {exc}"},
                HTTPStatus.BAD_GATEWAY,
            )
            return
        self._send_json(
            {
                "enabled": True,
                "authenticated": True,
                "snapshot": snapshot.to_dict(),
            }
        )

    def _alert_for_observation(self, observation_id: str) -> dict[str, Any] | None:
        for alert in self._store().list_alerts():
            if alert.get("source_observation_id") == observation_id:
                return alert
        return None

    def _add_channel_line(self, payload: dict[str, Any]) -> dict[str, Any]:
        line = str(payload.get("line") or payload.get("raw_line") or "").strip()
        if not line:
            raise ValueError("line is required")

        channel = str(
            payload.get("channel") or payload.get("source_instance") or ""
        ).strip()
        parsed = parse_chat_line(line, channel=channel)
        if parsed is None:
            return {
                "ok": True,
                "ignored": True,
                "reason": "not a chat message",
            }

        observation_payload = parsed.to_observation_payload()
        metadata = dict(observation_payload.get("metadata") or {})
        metadata["raw_line"] = line
        observation_payload["metadata"] = metadata
        observation = self._store().add_observation(observation_payload)
        return {
            "ok": True,
            "ignored": False,
            "parsed": observation_payload,
            "observation": observation.to_dict(),
            "alert": self._alert_for_observation(observation.observation_id),
        }

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _read_optional_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _parse_optional_int(self, raw: str) -> int | None:
        raw = raw.strip()
        if not raw:
            return None
        value = int(raw)
        if value < 0:
            raise ValueError("limit must be non-negative")
        return value

    def _parse_optional_float(self, raw: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        value = float(raw)
        if value < 0:
            raise ValueError("timeout must be non-negative")
        return value

    def _parse_alert_filters(
        self,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        min_level = str(query.get("min_level", [""])[0] or "").strip()
        if min_level and min_level.casefold() not in {
            "low",
            "medium",
            "high",
            "critical",
        }:
            raise ValueError(
                "min_level must be one of low, medium, high, or critical"
            )
        return {
            "acknowledged": self._parse_optional_bool(
                query.get("acknowledged", [""])[0],
                "acknowledged",
            ),
            "min_score": self._parse_optional_int_param(
                query.get("min_score", [""])[0],
                "min_score",
            ),
            "min_level": min_level,
        }

    def _parse_optional_int_param(self, raw: str, label: str) -> int | None:
        raw = raw.strip()
        if not raw:
            return None
        value = int(raw)
        if value < 0:
            raise ValueError(f"{label} must be non-negative")
        return value

    def _parse_optional_bool(self, raw: str, label: str) -> bool | None:
        value = raw.strip().casefold()
        if not value:
            return None
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"{label} must be true or false")

    def _parse_optional_bool_default(
        self,
        raw: str,
        default: bool,
        label: str,
    ) -> bool:
        value = self._parse_optional_bool(raw, label)
        return default if value is None else value

    def _parse_path_int(self, path: str, prefix: str, label: str) -> int:
        raw = unquote(path[len(prefix):]).strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} must be a positive integer") from exc
        if value <= 0:
            raise ValueError(f"{label} must be a positive integer")
        return value

    def _stream_events(
        self,
        since: str = "",
        limit: int = 50,
        timeout_seconds: float = 30.0,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_seen = since.strip()
        sent_ids: set[str] = set()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                alerts = self._store().list_alerts(
                    since=last_seen,
                    limit=limit,
                    acknowledged=acknowledged,
                    min_score=min_score,
                    min_level=min_level,
                )
                for alert in reversed(alerts):
                    alert_id = str(alert.get("id") or "")
                    if not alert_id or alert_id in sent_ids:
                        continue
                    self._write_sse("alert", alert_id, alert)
                    sent_ids.add(alert_id)
                    created_at = str(alert.get("created_at") or "")
                    if created_at > last_seen:
                        last_seen = created_at
            except (BrokenPipeError, ConnectionResetError):
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    def _write_sse(
        self,
        event_name: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        body = f"id: {event_id}\nevent: {event_name}\ndata: {data}\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_common_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_optional_json(
        self,
        key: str,
        payload: dict[str, Any] | None,
        missing_message: str,
    ) -> None:
        if payload is None:
            self._send_json({"error": missing_message}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({key: payload})

    def _send_text(
        self,
        text: str,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self._send_common_headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_common_headers(
        self,
        content_type: str | None,
        content_length: int,
    ) -> None:
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
