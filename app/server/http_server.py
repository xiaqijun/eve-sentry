"""Local HTTP server exposing the hostile intel map and JSON API."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.channels.parser import parse_chat_line
from app.esi.sso import EsiSsoError
from app.server.intel_store import IntelStore, utc_now_iso
from app.server.star_map_page import INDEX_HTML

logger = logging.getLogger(__name__)
API_V1_PREFIX = "/api/v1"


class IntelHTTPServer:
    """Small background HTTP server for local intel sharing."""

    def __init__(
        self,
        store: IntelStore,
        host: str = "127.0.0.1",
        port: int = 8765,
        config_store: Any | None = None,
        esi_session: Any | None = None,
        esi_config: dict[str, Any] | None = None,
        map_config_store: Any | None = None,
        esi_login: Any | None = None,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.config_store = config_store
        self.esi_session = esi_session
        self.esi_config = dict(esi_config or {})
        self.esi_login = esi_login
        self.map_config_store = map_config_store
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
        self._httpd.esi_config = self.esi_config  # type: ignore[attr-defined]
        self._httpd.esi_login = self.esi_login  # type: ignore[attr-defined]
        self._httpd.map_config_store = self.map_config_store  # type: ignore[attr-defined]
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
        if path.startswith(API_V1_PREFIX):
            self._handle_v1_get(parsed)
            return
        if path in {"/", "/index.html"}:
            self._send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._send_json({"health": self._health_payload()})
            return
        if path == "/api/heartbeats":
            self._send_json(self._store().heartbeat_snapshot())
            return
        if path == "/api/config":
            config_store = self._config_store()
            if config_store is None:
                self._send_json({"error": "config not enabled"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"config": config_store.to_dict()})
            return
        if path == "/api/map/config":
            map_config_store = self._map_config_store()
            if map_config_store is None:
                self._send_json({"error": "map config not enabled"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"map": map_config_store.to_dict()})
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
        if path.startswith("/api/systems/"):
            try:
                system_id = self._parse_path_int(
                    path,
                    "/api/systems/",
                    "system_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "system",
                self._store().system_profile(system_id),
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
        if path.startswith("/api/intel/character/"):
            self._send_entity_intel(
                path,
                parsed.query,
                "/api/intel/character/",
                "character_id",
                self._store().character_intel,
            )
            return
        if path.startswith("/api/intel/system/"):
            self._send_entity_intel(
                path,
                parsed.query,
                "/api/intel/system/",
                "system_id",
                self._store().system_intel,
            )
            return
        if path.startswith("/api/intel/corporation/"):
            self._send_entity_intel(
                path,
                parsed.query,
                "/api/intel/corporation/",
                "corporation_id",
                self._store().corporation_intel,
            )
            return
        if path.startswith("/api/intel/alliance/"):
            self._send_entity_intel(
                path,
                parsed.query,
                "/api/intel/alliance/",
                "alliance_id",
                self._store().alliance_intel,
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
                parsed_timeout = self._parse_optional_float_param(
                    query.get("timeout", [""])[0],
                    "timeout",
                )
                parsed_heartbeat = self._parse_optional_float_param(
                    query.get("heartbeat", [""])[0],
                    "heartbeat",
                )
                filters = self._parse_alert_filters(query)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            since, resume_after_id, include_since = self._event_stream_cursor(
                query.get("since", [""])[0]
            )
            self._stream_events(
                since=since,
                resume_after_id=resume_after_id,
                include_since=include_since,
                limit=50 if parsed_limit is None else parsed_limit,
                timeout_seconds=30.0 if parsed_timeout is None else parsed_timeout,
                heartbeat_seconds=15.0 if parsed_heartbeat is None else parsed_heartbeat,
                **filters,
            )
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith(API_V1_PREFIX):
            self._handle_v1_post(path)
            return
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
        if path == "/api/heartbeats":
            try:
                heartbeat = self._store().record_heartbeat(self._read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "heartbeat": heartbeat}, HTTPStatus.CREATED)
            return
        if path == "/api/map/refresh":
            map_config_store = self._map_config_store()
            if map_config_store is None:
                self._send_json({"error": "map config not enabled"}, HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._read_optional_json()
                if payload:
                    map_config_store.update(payload)
                config = map_config_store.refresh_from_source(
                    resolver=getattr(self._store(), "_resolver", None)
                )
                systems, links = map_config_store.build_map(
                    resolver=getattr(self._store(), "_resolver", None),
                    refresh_if_needed=False,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                return

            self._store().set_map_data(systems, links, allow_unmapped_systems=False)
            self._send_json(
                {
                    "ok": True,
                    "map": config,
                    "counts": {
                        "systems": len(systems),
                        "links": len(links),
                    },
                }
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
        if path.startswith(API_V1_PREFIX):
            self._handle_v1_put(path)
            return
        if path == "/api/map/config":
            map_config_store = self._map_config_store()
            if map_config_store is None:
                self._send_json({"error": "map config not enabled"}, HTTPStatus.NOT_FOUND)
                return

            try:
                payload = self._read_json()
                config = map_config_store.update(payload)
                systems, links = map_config_store.build_map(
                    resolver=getattr(self._store(), "_resolver", None),
                    refresh_if_needed=config.get("source") in {"sde", "esi"},
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            self._store().set_map_data(systems, links, allow_unmapped_systems=False)
            self._send_json(
                {
                    "ok": True,
                    "map": map_config_store.to_dict(),
                    "counts": {
                        "systems": len(systems),
                        "links": len(links),
                    },
                }
            )
            return

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
        if path.startswith(f"{API_V1_PREFIX}/reports/"):
            report_id = unquote(path[len(f"{API_V1_PREFIX}/reports/"):]).strip()
            if not self._store().delete_report(report_id):
                self._send_json({"error": "report not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "id": report_id})
            return
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

    def _handle_v1_get(self, parsed) -> None:
        path = parsed.path
        if path == f"{API_V1_PREFIX}/bootstrap":
            self._send_json({"bootstrap": self._bootstrap_payload()})
            return
        if path == f"{API_V1_PREFIX}/map":
            self._send_json({"map": self._map_snapshot_payload()})
            return
        if path == f"{API_V1_PREFIX}/clients":
            self._send_json({"clients": self._store().heartbeat_snapshot()})
            return
        if path == f"{API_V1_PREFIX}/active-intel":
            self._send_active_intel(parsed.query)
            return
        if path == f"{API_V1_PREFIX}/config":
            config_store = self._config_store()
            if config_store is None:
                self._send_json({"error": "config not enabled"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"config": config_store.to_dict()})
            return
        if path == f"{API_V1_PREFIX}/esi/status":
            self._send_json(self._esi_status_payload())
            return
        if path == f"{API_V1_PREFIX}/esi/login":
            esi_login = self._esi_login()
            if esi_login is None or not hasattr(esi_login, "snapshot"):
                self._send_json(
                    {"error": "ESI login not configured"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"login": esi_login.snapshot()})
            return
        if path == f"{API_V1_PREFIX}/esi/session":
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
        if path.startswith(f"{API_V1_PREFIX}/characters/by-name/"):
            name = unquote(path[len(f"{API_V1_PREFIX}/characters/by-name/"):]).strip()
            self._send_optional_json(
                "character",
                self._store().character_by_name(name),
                "character not found or ESI not enabled",
            )
            return
        if path.startswith(f"{API_V1_PREFIX}/characters/"):
            try:
                character_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/characters/",
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
        if path == f"{API_V1_PREFIX}/systems":
            self._send_json({"map": self._map_snapshot_payload()})
            return
        if path.startswith(f"{API_V1_PREFIX}/systems/by-name/"):
            name = unquote(path[len(f"{API_V1_PREFIX}/systems/by-name/"):]).strip()
            self._send_optional_json(
                "system",
                self._store().system_by_name(name),
                "system not found or ESI not enabled",
            )
            return
        if path.startswith(f"{API_V1_PREFIX}/systems/"):
            try:
                system_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/systems/",
                    "system_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_optional_json(
                "system",
                self._store().system_profile(system_id),
                "system not found or ESI not enabled",
            )
            return
        if path.startswith(f"{API_V1_PREFIX}/map/systems/"):
            try:
                system_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/map/systems/",
                    "system_id",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            payload = self._map_system_payload(system_id)
            if payload is None:
                self._send_json({"error": "system not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"system": payload})
            return
        if path.startswith(f"{API_V1_PREFIX}/kill-activity/character/"):
            try:
                character_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/kill-activity/character/",
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
        if path.startswith(f"{API_V1_PREFIX}/kill-activity/system/"):
            try:
                system_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/kill-activity/system/",
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
        if path.startswith(f"{API_V1_PREFIX}/kill-activity/corporation/"):
            try:
                corporation_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/kill-activity/corporation/",
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
        if path.startswith(f"{API_V1_PREFIX}/kill-activity/alliance/"):
            try:
                alliance_id = self._parse_path_int(
                    path,
                    f"{API_V1_PREFIX}/kill-activity/alliance/",
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
        if path == f"{API_V1_PREFIX}/reports":
            self._send_report_list(parsed.query)
            return
        if path == f"{API_V1_PREFIX}/observations":
            self._send_observation_list(parsed.query)
            return
        if path == f"{API_V1_PREFIX}/alerts":
            self._send_alert_list(parsed.query, active_only=True)
            return
        if path.startswith(f"{API_V1_PREFIX}/alerts/"):
            alert_id = unquote(path[len(f"{API_V1_PREFIX}/alerts/"):]).strip()
            detail = self._store().alert_detail(alert_id)
            if detail is None:
                self._send_json({"error": "alert not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"detail": detail})
            return
        if path == f"{API_V1_PREFIX}/events":
            query = parse_qs(parsed.query)
            try:
                parsed_limit = self._parse_optional_int(query.get("limit", [""])[0])
                parsed_timeout = self._parse_optional_float_param(
                    query.get("timeout", [""])[0],
                    "timeout",
                )
                parsed_heartbeat = self._parse_optional_float_param(
                    query.get("heartbeat", [""])[0],
                    "heartbeat",
                )
                filters = self._parse_alert_filters(query)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            since, resume_after_id, include_since = self._event_stream_cursor(
                query.get("since", [""])[0]
            )
            self._stream_events(
                since=since,
                resume_after_id=resume_after_id,
                include_since=include_since,
                limit=50 if parsed_limit is None else parsed_limit,
                timeout_seconds=30.0 if parsed_timeout is None else parsed_timeout,
                heartbeat_seconds=15.0 if parsed_heartbeat is None else parsed_heartbeat,
                active_only=True,
                **filters,
            )
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_v1_post(self, path: str) -> None:
        if path == f"{API_V1_PREFIX}/esi/login":
            esi_login = self._esi_login()
            if esi_login is None or not hasattr(esi_login, "start"):
                self._send_json(
                    {"error": "ESI login not configured"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            try:
                login = esi_login.start()
            except EsiSsoError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            except Exception as exc:
                self._send_json(
                    {"error": f"ESI login unavailable: {exc}"},
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            self._send_json({"ok": True, "login": login})
            return

        ack_prefix = f"{API_V1_PREFIX}/alerts/"
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
        if path == f"{API_V1_PREFIX}/channel-lines":
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
        if path == f"{API_V1_PREFIX}/ocr/snapshot":
            try:
                result = self._store().record_ocr_snapshot(self._read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            status = HTTPStatus.CREATED if result.get("created") else HTTPStatus.OK
            self._send_json(result, status)
            return
        if path == f"{API_V1_PREFIX}/clients/heartbeats":
            try:
                heartbeat = self._store().record_heartbeat(self._read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "heartbeat": heartbeat}, HTTPStatus.CREATED)
            return
        if path in {f"{API_V1_PREFIX}/reports", f"{API_V1_PREFIX}/observations"}:
            self._handle_v1_ingest(path)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_v1_put(self, path: str) -> None:
        if path != f"{API_V1_PREFIX}/config":
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

    def _handle_v1_ingest(self, path: str) -> None:
        try:
            payload = self._read_json()
            if path.endswith("/observations"):
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

    def _bootstrap_payload(self) -> dict[str, Any]:
        snapshot = self._runtime_snapshot()
        return {
            "schema_version": "intel_bootstrap.v1",
            "generated_at": snapshot.get("generated_at", ""),
            "map": self._map_snapshot_from_snapshot(snapshot),
            "reports": snapshot.get("reports", []),
            "observations": snapshot.get("observations", []),
            "alerts": snapshot.get("alerts", []),
            "active_intel": snapshot.get("active_intel", []),
            "clients": self._store().heartbeat_snapshot(),
            "config": self._config_store().to_dict() if self._config_store() else None,
            "esi": self._esi_status_payload(),
        }

    def _map_snapshot_payload(self) -> dict[str, Any]:
        return self._map_snapshot_from_snapshot(
            self._runtime_snapshot(include_reports=False, include_alerts=False)
        )

    def _runtime_snapshot(
        self,
        include_reports: bool = True,
        include_alerts: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        store = self._store()
        active_items = self._visible_active_items(store, store.list_active_intel())
        system_intel = store._aggregate_active_by_system(active_items)
        with store._lock:
            system_items = dict(store._systems)
            link_items = list(store._links)
            heartbeat_count = len(store._heartbeats)
            report_items = list(store._reports) if include_reports else []

        systems = []
        for name, system in sorted(system_items.items()):
            data = system.to_dict()
            data.update(system_intel.get(name, store._empty_system_intel()))
            if isinstance(data["hostiles"], set):
                data["hostiles"] = sorted(data["hostiles"])
            systems.append(data)

        reports = []
        observations = []
        if include_reports:
            report_items = store._visible_reports(report_items)
            recent_reports = sorted(
                report_items,
                key=lambda report: report.seen_at,
                reverse=True,
            )
            reports = [report.to_dict() for report in recent_reports[:limit]]
            observations = [
                report.to_observation().to_dict() for report in recent_reports[:limit]
            ]

        alerts = (
            self._active_alerts_from_reports(store, report_items, active_items, limit)
            if include_alerts
            else []
        )
        return {
            "generated_at": utc_now_iso(),
            "systems": systems,
            "links": [
                {"from": source, "to": target}
                for source, target in link_items
                if source in system_items and target in system_items
            ],
            "reports": reports,
            "observations": observations,
            "alerts": alerts,
            "active_intel": active_items,
            "summary": {
                "system_count": len(system_items),
                "active_system_count": sum(
                    1
                    for name, data in system_intel.items()
                    if name in system_items and data["hostile_count"]
                ),
                "report_count": len(report_items),
                "observation_count": len(report_items),
                "alert_count": len(alerts),
                "hostile_count": sum(
                    len(data["hostiles"])
                    for name, data in system_intel.items()
                    if name in system_items
                ),
                "heartbeat_count": heartbeat_count,
            },
        }

    def _visible_active_items(
        self,
        store: IntelStore,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        scorer = getattr(store, "_scorer", None)
        if not bool(getattr(scorer, "suppress_whitelisted_reports", True)):
            return list(items)
        watchlist = getattr(scorer, "watchlist", None)
        whitelist = getattr(watchlist, "whitelist", None)
        with store._lock:
            reports = list(store._reports)
        whitelist_names = {str(name).casefold() for name in whitelist or []}
        all_source_ids = {str(report.report_id) for report in reports}
        visible_source_ids = {
            str(report.report_id) for report in store._visible_reports(reports)
        }
        hidden_source_ids = all_source_ids - visible_source_ids
        visible = []
        for item in items:
            name_key = str(item.get("name") or "").casefold()
            if name_key in whitelist_names:
                continue
            if self._active_item_only_has_hidden_sources(item, hidden_source_ids):
                continue
            visible.append(item)
        return visible

    def _active_item_only_has_hidden_sources(
        self,
        item: dict[str, Any],
        hidden_source_ids: set[str],
    ) -> bool:
        source_ids = self._active_item_source_ids(item)
        return bool(source_ids) and all(
            source_id in hidden_source_ids for source_id in source_ids
        )

    def _active_item_source_ids(self, item: dict[str, Any]) -> list[str]:
        raw_source_ids = item.get("source_observation_ids", [])
        if raw_source_ids is None:
            return []
        if isinstance(raw_source_ids, str):
            raw_source_ids = [raw_source_ids]
        if not isinstance(raw_source_ids, list):
            return []
        return [str(source_id) for source_id in raw_source_ids if source_id]

    def _active_alerts_from_reports(
        self,
        store: IntelStore,
        reports: list[Any],
        active_items: list[dict[str, Any]],
        limit: int | None,
    ) -> list[dict[str, Any]]:
        _ = reports
        active_by_source_id: dict[str, dict[str, Any]] = {}
        for item in active_items:
            for source_id in self._active_item_source_ids(item):
                active_by_source_id[source_id] = item

        alerts = []
        for alert in store.list_alerts(limit=None):
            source_id = str(alert.get("source_observation_id") or "")
            active_item = active_by_source_id.get(source_id)
            if active_item is None:
                continue
            data = dict(alert)
            data["active_intel_id"] = active_item.get("id")
            alerts.append(data)

        alerts.sort(key=lambda alert: alert["created_at"], reverse=True)
        if limit is not None:
            alerts = alerts[:max(0, limit)]
        return alerts

    def _map_snapshot_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        summary = snapshot.get("summary")
        systems = snapshot.get("systems", [])
        links = snapshot.get("links", [])
        allowed_systems = self._configured_map_system_keys()
        if allowed_systems:
            systems = [
                system
                for system in systems
                if self._map_system_matches_keys(system, allowed_systems)
            ]
            allowed_names = {
                str(system.get("name") or "").strip()
                for system in systems
                if isinstance(system, dict)
            }
            links = [
                link
                for link in links
                if isinstance(link, dict)
                and str(link.get("from") or "").strip() in allowed_names
                and str(link.get("to") or "").strip() in allowed_names
            ]
        return {
            "schema_version": "map_snapshot.v1",
            "generated_at": snapshot.get("generated_at", ""),
            "systems": systems,
            "links": links,
            "summary": summary if isinstance(summary, dict) else {},
        }

    def _configured_map_system_keys(self) -> set[tuple[str, str]]:
        map_config_store = self._map_config_store()
        if map_config_store is None:
            return set()
        config = map_config_store.to_dict()
        systems = config.get("systems", [])
        if not isinstance(systems, list):
            return set()
        keys: set[tuple[str, str]] = set()
        for system in systems:
            if not isinstance(system, dict):
                continue
            system_id = self._optional_positive_int(system.get("system_id"))
            if system_id is not None:
                keys.add(("id", str(system_id)))
            name = str(system.get("name") or "").strip()
            if name:
                keys.add(("name", name.casefold()))
        return keys

    def _map_system_matches_keys(
        self,
        system: Any,
        keys: set[tuple[str, str]],
    ) -> bool:
        if not isinstance(system, dict):
            return False
        system_id = self._optional_positive_int(system.get("system_id"))
        if system_id is not None and ("id", str(system_id)) in keys:
            return True
        name = str(system.get("name") or "").strip()
        return bool(name and ("name", name.casefold()) in keys)

    def _map_system_payload(self, system_id: int) -> dict[str, Any] | None:
        profile = self._store().system_profile(system_id)
        intel = self._store().system_intel(system_id)
        if not isinstance(profile, dict) or not isinstance(intel, dict):
            return None
        map_node = None
        profile_name = str(profile.get("name") or "").strip()
        for system in self._store().snapshot().get("systems", []):
            if not isinstance(system, dict):
                continue
            if self._optional_positive_int(system.get("system_id")) == system_id:
                map_node = system
                break
            if profile_name and str(system.get("name") or "").strip() == profile_name:
                map_node = system
                break
        return {
            "profile": profile,
            "map_node": map_node,
            "intel": intel,
        }

    def _send_report_list(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
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

    def _send_observation_list(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
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
        self._send_json({"observations": observations, "count": len(observations)})

    def _send_active_intel(self, raw_query: str = "") -> None:
        query = parse_qs(raw_query)
        try:
            limit = self._parse_optional_int(query.get("limit", [""])[0])
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        active = self._store().list_active_intel(
            source=query.get("source", [""])[0],
            system=query.get("system", [""])[0],
            active=True,
            limit=limit,
        )
        active = self._visible_active_items(self._store(), active)
        self._send_json(
            {
                "active_intel": active,
                "count": len(active),
                "generated_at": utc_now_iso(),
            }
        )

    def _send_alert_list(self, raw_query: str, active_only: bool = False) -> None:
        query = parse_qs(raw_query)
        try:
            limit = self._parse_optional_int(query.get("limit", [""])[0])
            filters = self._parse_alert_filters(query)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if active_only:
            try:
                alerts = self._active_alert_list(
                    since=query.get("since", [""])[0],
                    limit=limit,
                    **filters,
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
        else:
            alerts = self._store().list_alerts(
                since=query.get("since", [""])[0],
                limit=limit,
                **filters,
            )
        self._send_json({"alerts": alerts, "count": len(alerts)})

    def _active_alert_list(
        self,
        since: str | None = None,
        limit: int | None = None,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
        include_since: bool = False,
    ) -> list[dict[str, Any]]:
        store = self._store()
        active_items = self._visible_active_items(store, store.list_active_intel())
        alerts = self._active_alerts_from_reports(
            store,
            [],
            active_items,
            limit=None,
        )

        since_query = since.strip() if since else ""
        if since_query:
            if include_since:
                alerts = [
                    alert for alert in alerts
                    if alert["created_at"] >= since_query
                ]
            else:
                alerts = [
                    alert for alert in alerts
                    if alert["created_at"] > since_query
                ]

        min_score_value = store._optional_score(min_score)
        min_level_rank = store._alert_level_rank(min_level)
        alerts = [
            alert for alert in alerts
            if store._alert_passes_filters(
                alert,
                acknowledged=acknowledged,
                min_score=min_score_value,
                min_level_rank=min_level_rank,
            )
        ]
        if limit is not None:
            alerts = alerts[:max(0, limit)]
        return alerts

    def _store(self) -> IntelStore:
        return self.server.store  # type: ignore[attr-defined,no-any-return]

    def _config_store(self) -> Any | None:
        return self.server.config_store  # type: ignore[attr-defined,no-any-return]

    def _esi_session(self) -> Any | None:
        return self.server.esi_session  # type: ignore[attr-defined,no-any-return]

    def _esi_login(self) -> Any | None:
        return getattr(self.server, "esi_login", None)

    def _esi_config(self) -> dict[str, Any]:
        config = getattr(self.server, "esi_config", None)
        result = dict(config) if isinstance(config, dict) else {}
        token_file = str(result.get("token_file") or "").strip()
        if token_file:
            result["token_file_present"] = os.path.exists(token_file)
        return result

    def _esi_public_resolver(self) -> Any | None:
        resolver = getattr(self._store(), "_resolver", None)
        if resolver is None:
            return None
        if any(
            hasattr(resolver, name)
            for name in ("resolve_names", "character_profile", "system_profile")
        ):
            return resolver
        return None

    def _map_config_store(self) -> Any | None:
        return self.server.map_config_store  # type: ignore[attr-defined,no-any-return]

    def _esi_status_payload(self) -> dict[str, Any]:
        session = self._esi_session()
        public_enabled = self._esi_public_resolver() is not None
        config = self._esi_config()
        if session is None:
            if public_enabled:
                return {
                    "enabled": True,
                    "public": True,
                    "authenticated": False,
                    "session": False,
                    "config": config,
                }
            return {"enabled": False, "authenticated": False, "config": config}
        if not hasattr(session, "load_tokens"):
            return {
                "enabled": True,
                "public": public_enabled,
                "authenticated": False,
                "session": True,
                "config": config,
                "error": "ESI session cannot load tokens",
            }
        try:
            tokens = session.load_tokens(refresh_if_needed=False)
        except EsiSsoError as exc:
            return {
                "enabled": True,
                "public": public_enabled,
                "authenticated": False,
                "session": True,
                "config": config,
                "error": str(exc),
            }
        return {
            "enabled": True,
            "public": public_enabled,
            "authenticated": True,
            "session": True,
            "config": config,
            "character_id": tokens.character_id,
            "character_owner_hash": tokens.character_owner_hash,
            "scopes": list(tokens.scopes),
            "expires_at": tokens.expires_at,
            "expired": bool(tokens.is_expired()),
        }

    def _health_payload(self) -> dict[str, Any]:
        store = self._store()
        return {
            "ok": True,
            "schema_version": "health.v1",
            "generated_at": utc_now_iso(),
            "storage": self._storage_health(store),
            "config": self._config_health(),
            "map": self._map_health(store),
            "esi": self._esi_status_payload(),
            "killboard": self._killboard_health(store),
            "clients": store.heartbeat_summary(),
            "events": self._event_health(store),
        }

    def _storage_health(self, store: IntelStore) -> dict[str, Any]:
        path = getattr(store, "_db_path", None) or getattr(store, "_filepath", None)
        return {
            "type": type(store).__name__,
            "path": str(path) if path is not None else "",
            "writable": self._storage_path_writable(path),
        }

    def _storage_path_writable(self, path: Any) -> bool:
        if path is None:
            return True
        try:
            target = os.fspath(path)
            directory = os.path.dirname(target) or "."
            if os.path.exists(target):
                return os.access(target, os.W_OK)
            return os.path.isdir(directory) and os.access(directory, os.W_OK)
        except (TypeError, ValueError, OSError):
            return False

    def _config_health(self) -> dict[str, Any]:
        config_store = self._config_store()
        if config_store is None:
            return {"enabled": False}
        config = config_store.to_dict()
        return {
            "enabled": True,
            "path": str(getattr(config_store, "path", "")),
            "schema_version": config.get("schema_version", ""),
            "scoring_version": config.get("scoring_version", ""),
            "evidence_rule_count": len(config.get("evidence_rules") or []),
            "cooldown_seconds": config.get("cooldown_seconds"),
        }

    def _killboard_health(self, store: IntelStore) -> dict[str, Any]:
        return {"enabled": False}

    def _map_health(self, store: IntelStore) -> dict[str, Any]:
        map_config_store = self._map_config_store()
        active_system_count = len(getattr(store, "_systems", {}))
        active_link_count = len(getattr(store, "_links", []))
        if map_config_store is None:
            return {
                "enabled": False,
                "system_count": active_system_count,
                "link_count": active_link_count,
            }
        config = map_config_store.to_dict()
        return {
            "enabled": True,
            "path": str(getattr(map_config_store, "path", "")),
            "schema_version": config.get("schema_version", ""),
            "source": config.get("source", ""),
            "layout_mode": config.get("layout_mode", ""),
            "sde_path": config.get("sde_path", ""),
            "system_count": active_system_count,
            "link_count": active_link_count,
            "last_refreshed_at": config.get("last_refreshed_at", ""),
            "last_refresh_error": config.get("last_refresh_error", ""),
        }

    def _event_health(self, store: IntelStore) -> dict[str, Any]:
        _ = store
        return {
            "alert_query_ok": True,
            "sse": {
                "enabled": True,
                "path": "/api/v1/events",
                "legacy_path": "/api/events",
            },
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
        data = snapshot.to_dict()
        self._annotate_esi_location(data.get("location"))
        self._send_json(
            {
                "enabled": True,
                "authenticated": True,
                "snapshot": data,
            }
        )

    def _annotate_esi_location(self, location: Any) -> None:
        if not isinstance(location, dict):
            return
        system_id = self._optional_positive_int(location.get("solar_system_id"))
        if system_id is None:
            return
        try:
            profile = self._store().system_profile(system_id)
        except Exception:
            profile = None
        if not isinstance(profile, dict):
            return
        location.setdefault("solar_system", profile)
        name = str(profile.get("name") or "").strip()
        if name:
            location.setdefault("solar_system_name", name)

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
        defer_enrichment = self._payload_bool(payload.get("defer_enrichment"))
        if defer_enrichment:
            metadata["enrichment_deferred"] = True
        observation_payload["metadata"] = metadata
        observation = self._store().add_observation(observation_payload)
        alert = (
            None
            if defer_enrichment
            else self._alert_for_observation(observation.observation_id)
        )
        return {
            "ok": True,
            "ignored": False,
            "parsed": observation_payload,
            "observation": observation.to_dict(),
            "alert": alert,
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

    def _parse_optional_float_param(self, raw: str, label: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        value = float(raw)
        if value < 0:
            raise ValueError(f"{label} must be non-negative")
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

    def _send_entity_intel(
        self,
        path: str,
        raw_query: str,
        prefix: str,
        label: str,
        fetcher: Any,
    ) -> None:
        query = parse_qs(raw_query)
        try:
            entity_id = self._parse_path_int(path, prefix, label)
            limit = self._parse_optional_int(query.get("limit", [""])[0])
            filters = self._parse_alert_filters(query)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        payload = fetcher(
            entity_id,
            since=query.get("since", [""])[0],
            limit=limit,
            **filters,
        )
        if payload is None:
            self._send_json({"error": f"{label} not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"intel": payload})

    def _parse_optional_bool(self, raw: str, label: str) -> bool | None:
        value = raw.strip().casefold()
        if not value:
            return None
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"{label} must be true or false")

    def _payload_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
        return False

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

    def _optional_positive_int(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _event_stream_cursor(self, since: str) -> tuple[str, str, bool]:
        """Resolve explicit since or browser Last-Event-ID into a stream cursor."""
        since = str(since or "").strip()
        if since:
            return since, "", False

        last_event_id = str(self.headers.get("Last-Event-ID") or "").strip()
        if not last_event_id:
            return "", "", False

        cursor = self._store().alert_cursor(last_event_id)
        if cursor:
            return cursor, last_event_id, True

        if "T" in last_event_id and last_event_id[:1].isdigit():
            return last_event_id, "", False
        return "", "", False

    def _stream_events(
        self,
        since: str = "",
        resume_after_id: str = "",
        include_since: bool = False,
        limit: int = 50,
        timeout_seconds: float = 30.0,
        heartbeat_seconds: float = 15.0,
        acknowledged: bool | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
        active_only: bool = False,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_seen = since.strip()
        resume_after_id = resume_after_id.strip()
        sent_ids: set[str] = set()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        heartbeat_interval = max(0.0, heartbeat_seconds)
        next_heartbeat_at = (
            time.monotonic() + heartbeat_interval if heartbeat_interval else 0.0
        )
        while True:
            try:
                wrote_event = False
                current_include_since = bool(last_seen) and (
                    include_since or bool(sent_ids)
                )
                if active_only:
                    alerts = self._active_alert_list(
                        since=last_seen,
                        limit=limit,
                        include_since=current_include_since,
                        acknowledged=acknowledged,
                        min_score=min_score,
                        min_level=min_level,
                    )
                else:
                    alerts = self._store().list_alerts(
                        since=last_seen,
                        limit=limit,
                        include_since=current_include_since,
                        acknowledged=acknowledged,
                        min_score=min_score,
                        min_level=min_level,
                    )
                ordered_alerts = sorted(
                    alerts,
                    key=lambda item: str(item.get("created_at") or ""),
                )
                if resume_after_id and not any(
                    str(alert.get("id") or "") == resume_after_id
                    for alert in ordered_alerts
                ):
                    resume_after_id = ""
                for alert in ordered_alerts:
                    alert_id = str(alert.get("id") or "")
                    if not alert_id or alert_id in sent_ids:
                        continue
                    if resume_after_id:
                        sent_ids.add(alert_id)
                        if alert_id == resume_after_id:
                            resume_after_id = ""
                        continue
                    self._write_sse("alert", alert_id, alert)
                    wrote_event = True
                    sent_ids.add(alert_id)
                    created_at = str(alert.get("created_at") or "")
                    if created_at > last_seen:
                        last_seen = created_at
                now = time.monotonic()
                if heartbeat_interval and wrote_event:
                    next_heartbeat_at = now + heartbeat_interval
                if (
                    heartbeat_interval
                    and not wrote_event
                    and now >= next_heartbeat_at
                ):
                    self._write_sse_comment("keepalive")
                    next_heartbeat_at = now + heartbeat_interval
            except (BrokenPipeError, ConnectionResetError):
                return

            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            sleep_for = min(1.0, remaining)
            if heartbeat_interval:
                sleep_for = min(sleep_for, max(0.0, next_heartbeat_at - now))
            time.sleep(sleep_for)

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

    def _write_sse_comment(self, comment: str) -> None:
        body = f": {comment}\n\n"
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

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
