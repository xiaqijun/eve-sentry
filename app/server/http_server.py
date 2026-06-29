"""Local HTTP server exposing the hostile intel map and JSON API."""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

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
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
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
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/intel":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_json()
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

        self._send_json({"ok": True, "report": report.to_dict()}, HTTPStatus.CREATED)

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("intel-server: " + format, *args)

    def _store(self) -> IntelStore:
        return self.server.store  # type: ignore[attr-defined,no-any-return]

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
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
