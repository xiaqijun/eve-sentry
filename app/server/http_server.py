"""Local HTTP server exposing the hostile intel JSON API."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.channels.parser import parse_chat_line
from app.core.heartbeat import monitored_system_names
from app.esi.sso import EsiSsoError
from app.server.auth_http import AuthHttpMixin
from app.server.intel_store import IntelStore, utc_now_iso

logger = logging.getLogger(__name__)
access_logger = logging.getLogger(f"{__name__}.access")
API_V1_PREFIX = "/api/v1"
_EVENT_STREAM_CONDITION = threading.Condition()
_EVENT_STREAM_GENERATION = 0
_ACTIVE_EVENT_STREAMS = 0
_ACTIVE_EVENT_SNAPSHOT_INIT_LOCK = threading.Lock()
_ACTIVE_EVENT_SNAPSHOT_TTL_SECONDS = 1.0
SSE_AUTH_RECHECK_SECONDS = 30.0
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_QUERY_LIMIT = 1000
DEFAULT_QUERY_LIMIT = 100
MAX_SSE_TIMEOUT_SECONDS = 300.0
MAX_SSE_HEARTBEAT_SECONDS = 60.0
MAX_ACCESS_LOG_PATH_CHARS = 512
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_OCR_QUERY_LOCK = threading.Lock()
_OCR_QUERY_JOBS: dict[str, dict[str, Any]] = {}
_OCR_QUERY_TTL_SECONDS = 60.0
_OCR_QUERY_RETENTION_SECONDS = 120.0


class RequestBodyError(ValueError):
    """Request body error with the HTTP status that should be returned."""

    def __init__(
        self,
        message: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.status = status


def _request_error_status(exc: ValueError) -> HTTPStatus:
    return getattr(exc, "status", HTTPStatus.BAD_REQUEST)


def _prune_ocr_query_jobs(now: float | None = None) -> None:
    current = time.monotonic() if now is None else float(now)
    expired = [
        query_id
        for query_id, job in _OCR_QUERY_JOBS.items()
        if current >= float(job.get("retained_until", job.get("deadline", 0.0)))
    ]
    for query_id in expired:
        _OCR_QUERY_JOBS.pop(query_id, None)


def _online_detector_client_ids(snapshot: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for heartbeat in snapshot.get("heartbeats", []):
        if not isinstance(heartbeat, dict):
            continue
        if str(heartbeat.get("client_type") or "").strip() != "detector_client":
            continue
        if not bool(heartbeat.get("online")):
            continue
        details = heartbeat.get("details")
        if not isinstance(details, dict) or not bool(details.get("monitoring")):
            continue
        heartbeat_client_id = str(heartbeat.get("client_id") or "").strip()
        targets = details.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [details]
        for target in targets:
            if not isinstance(target, dict) or not bool(target.get("monitoring", True)):
                continue
            client_id = str(target.get("client_id") or heartbeat_client_id).strip()
            if client_id and client_id not in result:
                result.append(client_id)
    return result


def _online_detector_targets(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Return monitored target IDs and their heartbeat parent IDs."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for heartbeat in snapshot.get("heartbeats", []):
        if not isinstance(heartbeat, dict):
            continue
        if str(heartbeat.get("client_type") or "").strip() != "detector_client":
            continue
        if not bool(heartbeat.get("online")):
            continue
        details = heartbeat.get("details")
        if not isinstance(details, dict) or not bool(details.get("monitoring")):
            continue
        heartbeat_client_id = str(heartbeat.get("client_id") or "").strip()
        targets = details.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [details]
        for target in targets:
            if not isinstance(target, dict) or not bool(target.get("monitoring", True)):
                continue
            client_id = str(target.get("client_id") or heartbeat_client_id).strip()
            if not client_id or client_id in seen:
                continue
            seen.add(client_id)
            result.append(
                {
                    "client_id": client_id,
                    "heartbeat_client_id": heartbeat_client_id or client_id,
                }
            )
    return result


def _create_ocr_query(
    snapshot: dict[str, Any],
    filters: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    now = time.monotonic()
    query_id = f"ocrq_{uuid.uuid4().hex}"
    targets = _online_detector_targets(snapshot)
    if not targets:
        raise RequestBodyError("没有在线监控节点", HTTPStatus.CONFLICT)
    ttl_seconds = max(5.0, min(_OCR_QUERY_TTL_SECONDS, timeout_seconds))
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).isoformat()
    job = {
        "query_id": query_id,
        "created_at": utc_now_iso(),
        "expires_at": expires_at,
        "deadline": now + ttl_seconds,
        "retained_until": now + ttl_seconds + _OCR_QUERY_RETENTION_SECONDS,
        "filters": dict(filters),
        "clients": {
            target["client_id"]: {
                "claimed": False,
                "heartbeat_client_id": target["heartbeat_client_id"],
            }
            for target in targets
        },
        "results": {},
        "status": "pending",
    }
    with _OCR_QUERY_LOCK:
        _prune_ocr_query_jobs(now)
        _OCR_QUERY_JOBS[query_id] = job
    return {
        "query_id": query_id,
        "status": "pending",
        "requested_clients": [target["client_id"] for target in targets],
        "expires_at": expires_at,
    }


def _claim_ocr_query_commands(client_id: str) -> list[dict[str, Any]]:
    normalized = str(client_id or "").strip()
    if not normalized:
        return []
    now = time.monotonic()
    commands: list[dict[str, Any]] = []
    with _OCR_QUERY_LOCK:
        _prune_ocr_query_jobs(now)
        for job in _OCR_QUERY_JOBS.values():
            if job.get("status") != "pending" or now >= float(job.get("deadline", 0.0)):
                continue
            for target_client_id, client in job.get("clients", {}).items():
                if not isinstance(client, dict):
                    continue
                if client.get("heartbeat_client_id") != normalized:
                    continue
                if client.get("claimed"):
                    continue
                client["claimed"] = True
                commands.append(
                    {
                        "command": "ocr_query",
                        "query_id": str(job["query_id"]),
                        "target_client_id": target_client_id,
                        "filters": dict(job.get("filters") or {}),
                        "expires_at": job["expires_at"],
                    }
                )
    return commands


def _query_client_matches(expected: str, actual: str) -> bool:
    return actual == expected or actual.startswith(f"{expected}:")


def _complete_ocr_query(
    query_id: str,
    client_id: str,
    payload: dict[str, Any],
    server_result: dict[str, Any],
    store: Any | None = None,
) -> None:
    normalized_query = str(query_id or "").strip()
    normalized_client = str(client_id or "").strip()
    if not normalized_query or not normalized_client:
        return
    now = time.monotonic()
    with _OCR_QUERY_LOCK:
        _prune_ocr_query_jobs(now)
        job = _OCR_QUERY_JOBS.get(normalized_query)
        if not isinstance(job, dict) or job.get("status") != "pending":
            return
        expected = next(
            (
                expected_id
                for expected_id in job.get("clients", {})
                if _query_client_matches(expected_id, normalized_client)
            ),
            None,
        )
        if expected is None:
            return
        active_rows: list[dict[str, Any]] = []
        if store is not None:
            try:
                rows = store.list_active_intel()
            except Exception:
                rows = []
            system_name = str(payload.get("system_name") or "").strip().casefold()
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("active") is False:
                    continue
                metadata = row.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                row_client = str(metadata.get("client_id") or "").strip()
                if not _query_client_matches(normalized_client, row_client):
                    continue
                if system_name and str(row.get("system_name") or "").strip().casefold() != system_name:
                    continue
                active_rows.append(dict(row))
        job["results"][normalized_client] = {
            "client_id": normalized_client,
            "system_name": str(payload.get("system_name") or "Unknown"),
            "names": [str(name) for name in payload.get("names", []) if str(name).strip()],
            "received_at": utc_now_iso(),
            "server_result": dict(server_result),
            "recognized": active_rows,
        }
        if len(job["results"]) >= len(job.get("clients", {})):
            job["status"] = "completed"


def _refresh_ocr_query_results(job: dict[str, Any], store: Any | None) -> None:
    if store is None:
        return
    try:
        rows = store.list_active_intel()
    except Exception:
        return
    if not isinstance(rows, list):
        return
    for result in job.get("results", {}).values():
        client_id = str(result.get("client_id") or "").strip()
        system_name = str(result.get("system_name") or "").strip().casefold()
        recognized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("active") is False:
                continue
            metadata = row.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            row_client = str(metadata.get("client_id") or "").strip()
            if not _query_client_matches(client_id, row_client):
                continue
            if system_name and str(row.get("system_name") or "").strip().casefold() != system_name:
                continue
            recognized.append(dict(row))
        result["recognized"] = recognized


def _ocr_query_status(query_id: str, store: Any | None = None) -> dict[str, Any] | None:
    normalized = str(query_id or "").strip()
    now = time.monotonic()
    with _OCR_QUERY_LOCK:
        _prune_ocr_query_jobs(now)
        job = _OCR_QUERY_JOBS.get(normalized)
        if not isinstance(job, dict):
            return None
        _refresh_ocr_query_results(job, store)
        if job.get("status") == "pending" and now >= float(job.get("deadline", 0.0)):
            job["status"] = "completed" if job.get("results") else "timed_out"
        expected = len(job.get("clients", {}))
        received = len(job.get("results", {}))
        return {
            "query_id": normalized,
            "status": job.get("status", "pending"),
            "created_at": job.get("created_at", ""),
            "expires_at": job.get("expires_at"),
            "requested_clients": sorted(job.get("clients", {}).keys()),
            "received_clients": received,
            "expected_clients": expected,
            "results": list(job.get("results", {}).values()),
        }


def _notify_event_streams() -> None:
    """Wake connected SSE clients after alert-producing state changes."""
    global _EVENT_STREAM_GENERATION
    with _EVENT_STREAM_CONDITION:
        _EVENT_STREAM_GENERATION += 1
        _EVENT_STREAM_CONDITION.notify_all()


def _event_stream_generation() -> int:
    with _EVENT_STREAM_CONDITION:
        return _EVENT_STREAM_GENERATION


def _wait_for_event_stream_change(generation: int, timeout: float) -> None:
    """Wait until ingestion changes alert state or the fallback timeout expires."""
    if timeout <= 0:
        return
    with _EVENT_STREAM_CONDITION:
        if _EVENT_STREAM_GENERATION == generation:
            _EVENT_STREAM_CONDITION.wait(timeout=timeout)


def _active_event_stream_count() -> int:
    """Return the number of SSE responses currently being served."""
    with _EVENT_STREAM_CONDITION:
        return _ACTIVE_EVENT_STREAMS


def _track_event_stream(method):
    """Track an event-stream handler for its full response lifetime."""

    @wraps(method)
    def tracked(*args, **kwargs):
        global _ACTIVE_EVENT_STREAMS
        with _EVENT_STREAM_CONDITION:
            _ACTIVE_EVENT_STREAMS += 1
        try:
            return method(*args, **kwargs)
        finally:
            with _EVENT_STREAM_CONDITION:
                _ACTIVE_EVENT_STREAMS = max(0, _ACTIVE_EVENT_STREAMS - 1)

    return tracked


def _next_monitoring_heartbeat_stale_in(client_snapshot: Any) -> float | None:
    """Return seconds until the next online monitoring node becomes stale."""
    if not isinstance(client_snapshot, dict):
        return None
    heartbeats = client_snapshot.get("heartbeats")
    if not isinstance(heartbeats, list):
        return None

    remaining: list[float] = []
    for heartbeat in heartbeats:
        if not isinstance(heartbeat, dict) or not heartbeat.get("online"):
            continue
        if not monitored_system_names({"heartbeats": [heartbeat]}):
            continue
        try:
            age_seconds = float(heartbeat.get("age_seconds", 0.0))
            stale_after_seconds = float(heartbeat["stale_after_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        remaining.append(max(0.0, stale_after_seconds - age_seconds))

    if not remaining:
        return None
    # The online check includes the exact deadline, so cross it before refreshing.
    return min(remaining) + 0.01


def _active_hostile_counts(
    alerts: list[dict[str, Any]],
    active_items: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Count active hostile alerts and detector presence by solar system."""
    counts: dict[str, int] = {}
    detector_counts: dict[str, dict[str, tuple[str, int]]] = {}
    display_names: dict[str, str] = {}

    def remember_detector(
        system_name: str,
        client_id: str,
        hostile_count: int,
        seen_at: str,
    ) -> None:
        system_key = system_name.casefold()
        current = detector_counts.setdefault(system_key, {}).get(client_id)
        if current is None or seen_at >= current[0]:
            detector_counts[system_key][client_id] = (seen_at, hostile_count)

    for alert in alerts:
        system_name = str(
            alert.get("system_name") or alert.get("system") or "Unknown"
        ).strip() or "Unknown"
        system_key = system_name.casefold()
        display_names.setdefault(system_key, system_name)
        detector_client_id = str(alert.get("detector_client_id") or "").strip()
        if detector_client_id:
            try:
                hostile_count = max(0, int(alert.get("hostile_count") or 0))
            except (TypeError, ValueError):
                hostile_count = 0
            remember_detector(
                system_name,
                detector_client_id,
                hostile_count,
                str(
                    alert.get("hostile_icon_seen_at")
                    or alert.get("created_at")
                    or ""
                ),
            )
            continue
        counts[system_key] = counts.get(system_key, 0) + 1

    for item in active_items or []:
        if not isinstance(item, dict) or not bool(item.get("active", True)):
            continue
        source = str(item.get("source") or "").strip().casefold()
        metadata = item.get("metadata")
        if source != "eve-sentry-detector" or not isinstance(metadata, dict):
            continue
        if "hostile_icon_count" not in metadata:
            continue
        system_name = str(
            item.get("system_name") or item.get("system") or "Unknown"
        ).strip() or "Unknown"
        display_names.setdefault(system_name.casefold(), system_name)
        try:
            hostile_count = max(0, int(metadata.get("hostile_icon_count") or 0))
        except (TypeError, ValueError):
            hostile_count = 0
        client_id = str(
            metadata.get("client_id")
            or item.get("source_instance")
            or "unknown"
        ).strip() or "unknown"
        remember_detector(
            system_name,
            client_id,
            hostile_count,
            str(
                metadata.get("hostile_icon_seen_at")
                or item.get("last_seen_at")
                or ""
            ),
        )

    for system_key, node_counts in detector_counts.items():
        if node_counts:
            counts[system_key] = counts.get(system_key, 0) + max(
                count for _, count in node_counts.values()
            )
    return {
        display_names.get(system_key, system_key): count
        for system_key, count in counts.items()
    }


def _split_query_values(values: list[str]) -> list[str]:
    """Normalize repeated or comma-separated query values."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for item in str(raw or "").split(","):
            value = item.strip()
            if value and value.casefold() not in seen:
                seen.add(value.casefold())
                result.append(value)
    return result


def _is_hostile_history_alert(alert: object) -> bool:
    """Keep friendly classification records out of the hostile history feed."""
    if not isinstance(alert, dict):
        return False

    classification = str(alert.get("classification") or "").strip().casefold()
    if classification == "white":
        return False
    if classification == "red":
        return True

    evidence = alert.get("evidence")
    if isinstance(evidence, list):
        evidence_types = {
            str(item.get("type") or "").strip().casefold()
            for item in evidence
            if isinstance(item, dict)
        }
        if any(item.startswith("friendly_") for item in evidence_types):
            return False
    return True


def _monitoring_target_state(client_snapshot: Any) -> list[dict[str, Any]]:
    """Return stable online account/location fields for SSE change detection."""
    if not isinstance(client_snapshot, dict):
        return []
    heartbeats = client_snapshot.get("heartbeats")
    if not isinstance(heartbeats, list):
        return []
    state: list[dict[str, Any]] = []
    for heartbeat in heartbeats:
        if not isinstance(heartbeat, dict):
            continue
        if str(heartbeat.get("client_type") or "") != "detector_client":
            continue
        if not bool(heartbeat.get("online")):
            continue
        details = heartbeat.get("details")
        if not isinstance(details, dict) or not bool(details.get("monitoring")):
            continue
        targets = details.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [details]
        details_system_name = str(
            details.get("system_name") or details.get("system") or ""
        ).strip()
        for target in targets:
            if not isinstance(target, dict) or not bool(
                target.get("monitoring", True)
            ):
                continue
            if target.get("capture_online") is False:
                # Keep the target in heartbeat management details, but do not
                # expose a window whose local capture connection is offline.
                continue
            if target.get("game_connection_online") is False:
                # The EVE process can still exist while its game-server
                # connection is down; do not publish that window as a live
                # monitoring node until the Gamelog reports recovery.
                continue
            target_system_name = str(
                target.get("system_name") or target.get("system") or ""
            ).strip()
            if target_system_name.casefold() in {"", "unknown", "未知星系"}:
                target_system_name = details_system_name
            if target_system_name.casefold() in {
                "",
                "unknown",
                "\u672a\u77e5\u661f\u7cfb",
            }:
                # Heartbeats may arrive before local chatlog detection. Keep
                # those clients online, but do not expose an unusable node in
                # the map or robot snapshot until its location is known.
                continue
            state.append(
                {
                    "heartbeat_client_id": str(
                        heartbeat.get("client_id") or ""
                    ).strip(),
                    "client_id": str(target.get("client_id") or "").strip(),
                    "character_name": str(
                        target.get("character_name") or ""
                    ).strip(),
                    "source_instance": str(
                        target.get("source_instance")
                        or target.get("window_title")
                        or ""
                    ).strip(),
                    "system_name": target_system_name,
                    "system_id": target.get("system_id"),
                }
            )
    return sorted(
        state,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _monitoring_nodes_version(nodes: list[dict[str, Any]]) -> str:
    """Return a stable version for an online monitoring-node snapshot."""
    encoded = json.dumps(
        nodes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _monitoring_node_key(node: dict[str, Any]) -> str:
    """Return a stable identity for one monitored account/window target."""
    client_id = str(node.get("client_id") or "").strip()
    if client_id:
        return f"client:{client_id}"
    heartbeat_id = str(node.get("heartbeat_client_id") or "").strip()
    source_instance = str(node.get("source_instance") or "").strip()
    if heartbeat_id or source_instance:
        return f"window:{heartbeat_id}:{source_instance}"
    character_name = str(node.get("character_name") or "").strip()
    return f"character:{character_name.casefold()}"


def _monitoring_node_changes(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe online, offline, and system-move changes between snapshots."""
    previous_by_key = {
        _monitoring_node_key(node): node
        for node in previous
        if isinstance(node, dict)
    }
    current_by_key = {
        _monitoring_node_key(node): node
        for node in current
        if isinstance(node, dict)
    }
    changes: list[dict[str, Any]] = []

    for key in sorted(current_by_key.keys() - previous_by_key.keys()):
        change = dict(current_by_key[key])
        change.update({"node_id": key, "change": "online"})
        changes.append(change)

    for key in sorted(previous_by_key.keys() - current_by_key.keys()):
        change = dict(previous_by_key[key])
        change.update({"node_id": key, "change": "offline"})
        changes.append(change)

    for key in sorted(current_by_key.keys() & previous_by_key.keys()):
        before = previous_by_key[key]
        after = current_by_key[key]
        before_system = str(
            before.get("system_name") or before.get("system") or ""
        ).strip()
        after_system = str(
            after.get("system_name") or after.get("system") or ""
        ).strip()
        if before_system.casefold() == after_system.casefold():
            continue
        change = dict(after)
        change.update(
            {
                "node_id": key,
                "change": "moved",
                "from_system": before_system or "Unknown",
                "to_system": after_system or "Unknown",
            }
        )
        changes.append(change)

    return changes


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
        auth_service: Any | None = None,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.config_store = config_store
        self.esi_session = esi_session
        self.esi_config = dict(esi_config or {})
        self.esi_login = esi_login
        self.auth_service = auth_service
        self.map_config_store = map_config_store
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        set_change_notifier = getattr(self.store, "set_change_notifier", None)
        if callable(set_change_notifier):
            set_change_notifier(_notify_event_streams)
        add_auth_listener = getattr(
            self.auth_service,
            "add_authorization_change_listener",
            None,
        )
        if callable(add_auth_listener):
            add_auth_listener(_notify_event_streams)

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

        Handler.auth_service = self.auth_service
        return Handler


class IntelRequestHandler(AuthHttpMixin, BaseHTTPRequestHandler):
    """Request handler for the local intel service."""

    server_version = "EveSentryIntel/1.0"

    def handle_one_request(self) -> None:
        """Serve one request and emit one structured access-log record."""
        self._request_id = uuid.uuid4().hex
        self._response_status = None
        started_at = time.perf_counter()
        try:
            super().handle_one_request()
        finally:
            if getattr(self, "raw_requestline", b""):
                self._log_access_request(started_at)

    def parse_request(self) -> bool:
        """Adopt a safe upstream request ID after parsing request headers."""
        parsed = super().parse_request()
        peer = str(getattr(self, "client_address", ("",))[0]).strip()
        try:
            peer_is_loopback = ip_address(peer).is_loopback
        except ValueError:
            peer_is_loopback = False
        if parsed and peer_is_loopback:
            request_id = str(self.headers.get("X-Request-ID") or "").strip()
            if _REQUEST_ID_PATTERN.fullmatch(request_id):
                self._request_id = request_id
        return parsed

    def end_headers(self) -> None:
        self.send_header("X-Request-ID", self._request_id)
        self.send_header("Access-Control-Expose-Headers", "X-Request-ID")
        super().end_headers()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Capture response status; final logging happens after the response."""
        _ = size
        try:
            self._response_status = int(code)
        except (TypeError, ValueError):
            self._response_status = None

    def _log_access_request(self, started_at: float) -> None:
        duration_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
        raw_path = str(getattr(self, "path", "") or "")
        try:
            path = urlparse(raw_path).path or "/"
        except ValueError:
            path = "/"
        path = path[:MAX_ACCESS_LOG_PATH_CHARS]
        record = {
            "event": "http_request",
            "request_id": self._request_id,
            "method": str(getattr(self, "command", "") or ""),
            "path": path,
            "status": self._response_status,
            "duration_ms": round(duration_ms, 3),
        }
        access_logger.info(
            json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        )

    def _send_auth_exception(self, exc: Exception) -> None:
        if isinstance(exc, RequestBodyError):
            self._send_json({"error": str(exc)}, exc.status)
            return
        super()._send_auth_exception(exc)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authorize_request("GET", path):
            return
        if self._handle_auth_get(path):
            return
        if path.startswith(API_V1_PREFIX):
            self._handle_v1_get(parsed)
            return
        if path == "/api/health":
            self._send_json({"health": self._health_payload()})
            return
        if path == "/api/livez":
            self._send_json({"ok": True})
            return
        if path == "/api/readyz":
            payload = self._readiness_payload()
            status = (
                HTTPStatus.OK
                if payload["ok"]
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._send_json(payload, status)
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
                limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
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
                limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
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
                limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
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
            (
                since,
                resume_after_id,
                include_since,
                stream_cursor,
            ) = self._event_stream_cursor(query.get("since", [""])[0])
            self._stream_events(
                since=since,
                resume_after_id=resume_after_id,
                stream_cursor=stream_cursor,
                include_since=include_since,
                limit=50 if parsed_limit is None else parsed_limit,
                timeout_seconds=parsed_timeout,
                heartbeat_seconds=15.0 if parsed_heartbeat is None else parsed_heartbeat,
                **filters,
            )
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorize_request("POST", path):
            return
        if self._handle_auth_post(path):
            return
        if path.startswith(API_V1_PREFIX):
            self._handle_v1_post(path)
            return
        if path == "/api/channel-lines":
            try:
                result = self._add_channel_line(self._read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            if not result.get("ignored"):
                _notify_event_streams()
            self._send_json(
                result,
                HTTPStatus.OK if result.get("ignored") else HTTPStatus.CREATED,
            )
            return
        if path == "/api/heartbeats":
            try:
                heartbeat = self._store().record_heartbeat(
                    self._attributed_heartbeat_payload(self._read_json())
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            _notify_event_streams()
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
                self._send_json({"error": str(exc)}, _request_error_status(exc))
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
                _notify_event_streams()
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
            _notify_event_streams()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, _request_error_status(exc))
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
        if not self._authorize_request("PUT", path):
            return
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
                self._send_json({"error": str(exc)}, _request_error_status(exc))
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
            self._send_json({"error": str(exc)}, _request_error_status(exc))
            return

        self._store().set_scorer(config.build_scorer())
        self._invalidate_identity_cache()
        self._send_json({"ok": True, "config": config.to_dict()})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorize_request("DELETE", path):
            return
        if self._handle_auth_delete(path):
            return
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
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-CSRF-Token",
        )
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("intel-server: " + format, *args)

    def _handle_v1_get(self, parsed) -> None:
        path = parsed.path
        if path.startswith(f"{API_V1_PREFIX}/ocr/query/"):
            query_id = unquote(path[len(f"{API_V1_PREFIX}/ocr/query/"):]).strip()
            status = _ocr_query_status(query_id, self._store())
            if status is None:
                self._send_json({"error": "OCR query not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(status)
            return
        if path == f"{API_V1_PREFIX}/bootstrap":
            self._send_json({"bootstrap": self._bootstrap_payload()})
            return
        if path == f"{API_V1_PREFIX}/map":
            self._send_json({"map": self._map_snapshot_payload()})
            return
        if path == f"{API_V1_PREFIX}/map/neighborhood":
            query = parse_qs(parsed.query)
            try:
                hops = self._parse_optional_int_param(
                    query.get("hops", [""])[0],
                    "hops",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            system_names = _split_query_values(query.get("systems", []))
            system_ids = _split_query_values(query.get("system_ids", []))
            self._send_json(
                {
                    "map": self._map_neighborhood_payload(
                        system_names=system_names,
                        system_ids=system_ids,
                        hops=3 if hops is None else hops,
                    )
                }
            )
            return
        if path == f"{API_V1_PREFIX}/clients":
            self._send_json({"clients": self._store().heartbeat_snapshot()})
            return
        if path == f"{API_V1_PREFIX}/active-intel":
            self._send_active_intel(parsed.query)
            return
        if path == f"{API_V1_PREFIX}/alert-history":
            self._send_alert_history(parsed.query)
            return
        if path == f"{API_V1_PREFIX}/hostile-waves":
            self._send_hostile_waves(parsed.query)
            return
        if path == f"{API_V1_PREFIX}/integrations/hostile-systems":
            self._send_hostile_systems()
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
                include_bootstrap = self._parse_optional_bool_default(
                    query.get("bootstrap", [""])[0],
                    False,
                    "bootstrap",
                )
                filters = self._parse_alert_filters(query)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            (
                since,
                resume_after_id,
                include_since,
                stream_cursor,
            ) = self._event_stream_cursor(query.get("since", [""])[0])
            self._stream_events(
                since=since,
                resume_after_id=resume_after_id,
                stream_cursor=stream_cursor,
                include_since=include_since,
                limit=50 if parsed_limit is None else parsed_limit,
                timeout_seconds=30.0 if parsed_timeout is None else parsed_timeout,
                heartbeat_seconds=15.0 if parsed_heartbeat is None else parsed_heartbeat,
                active_only=True,
                include_bootstrap=include_bootstrap,
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

        if path == f"{API_V1_PREFIX}/channel-lines":
            try:
                result = self._add_channel_line(self._read_json())
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            if not result.get("ignored"):
                _notify_event_streams()
            self._send_json(
                result,
                HTTPStatus.OK if result.get("ignored") else HTTPStatus.CREATED,
            )
            return
        if path == f"{API_V1_PREFIX}/ocr/query":
            try:
                payload = self._read_optional_json() or {}
                if not isinstance(payload, dict):
                    raise RequestBodyError("OCR query payload must be an object")
                filters = {
                    key: str(payload.get(key) or "").strip()
                    for key in ("name", "corporation", "alliance")
                    if str(payload.get(key) or "").strip()
                }
                timeout_seconds = float(payload.get("timeout_seconds") or 30.0)
                if timeout_seconds <= 0:
                    raise RequestBodyError("timeout_seconds must be positive")
                result = _create_ocr_query(
                    self._store().heartbeat_snapshot(),
                    filters,
                    timeout_seconds,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            self._send_json(result, HTTPStatus.ACCEPTED)
            return
        if path == f"{API_V1_PREFIX}/ocr/snapshot":
            try:
                store = self._store()
                payload = self._read_json()
                result = store.record_ocr_snapshot(payload)
                store.refresh_detector_heartbeat(payload.get("client_id"))
                _complete_ocr_query(
                    str(payload.get("query_id") or ""),
                    str(payload.get("client_id") or ""),
                    payload,
                    result,
                    store,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            _notify_event_streams()
            status = HTTPStatus.CREATED if result.get("created") else HTTPStatus.OK
            self._send_json(result, status)
            return
        if path == f"{API_V1_PREFIX}/hostile-presence":
            try:
                store = self._store()
                payload = self._read_json()
                result = store.record_hostile_presence(payload)
                store.refresh_detector_heartbeat(payload.get("client_id"))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            if result.get("accepted", True):
                _notify_event_streams()
            status = HTTPStatus.CREATED if result.get("created") else HTTPStatus.OK
            self._send_json(result, status)
            return
        if path == f"{API_V1_PREFIX}/clients/heartbeats":
            try:
                payload = self._attributed_heartbeat_payload(self._read_json())
                heartbeat = self._store().record_heartbeat(payload)
                commands = _claim_ocr_query_commands(payload.get("client_id"))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, _request_error_status(exc))
                return
            _notify_event_streams()
            self._send_json(
                {"ok": True, "heartbeat": heartbeat, "commands": commands},
                HTTPStatus.CREATED,
            )
            return
        if path in {f"{API_V1_PREFIX}/reports", f"{API_V1_PREFIX}/observations"}:
            self._handle_v1_ingest(path)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _attributed_heartbeat_payload(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace client-supplied heartbeat trust fields with request state."""
        service = self._auth_service()
        principal = self._auth_principal
        if service is not None and service.enforce_requests and (
            principal is None
            or principal.auth_type != "api_key"
            or principal.api_key_type != "desktop"
        ):
            raise RequestBodyError(
                "desktop API key is required",
                HTTPStatus.FORBIDDEN,
            )
        attributed = dict(payload)
        attributed["user_id"] = principal.user_id if principal is not None else ""
        attributed["api_key_id"] = (
            principal.api_key_id if principal is not None else ""
        )
        attributed["remote_ip"] = self._login_client_ip()
        attributed["seen_at"] = utc_now_iso()
        return attributed

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
            self._send_json({"error": str(exc)}, _request_error_status(exc))
            return
        self._store().set_scorer(config.build_scorer())
        self._invalidate_identity_cache()
        self._send_json({"ok": True, "config": config.to_dict()})

    def _handle_v1_ingest(self, path: str) -> None:
        try:
            payload = self._read_json()
            if path.endswith("/observations"):
                observation = self._store().add_observation(payload)
                _notify_event_streams()
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
            _notify_event_streams()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, _request_error_status(exc))
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
            "hostile_personnel": snapshot.get("hostile_personnel", []),
            "clients": self._store().heartbeat_snapshot(),
            "config": self._config_store().to_dict() if self._config_store() else None,
            "esi": self._esi_status_payload(),
        }

    def _public_esi_health(self) -> dict[str, Any]:
        status = self._esi_status_payload()
        public_status = {
            key: status[key]
            for key in (
                "enabled",
                "public",
                "authenticated",
                "session",
                "expired",
            )
            if key in status
        }
        config = status.get("config")
        if isinstance(config, dict):
            public_status["config"] = {
                key: config[key]
                for key in (
                    "client_id_configured",
                    "token_file_present",
                    "token_storage",
                )
                if key in config
            }
        if status.get("error"):
            public_status["degraded"] = True
        return public_status

    def _esi_gateway_observability(self) -> dict[str, Any]:
        """Return admin-only gateway and client metrics without credentials."""
        config = self._esi_config()
        resolver = self._esi_public_resolver()
        client = getattr(resolver, "client", None) if resolver is not None else None
        gateway = {
            "configured": callable(getattr(client, "gateway_health", None)),
            "reachable": False,
            "url": str(config.get("gateway_url") or ""),
            "checked_at": utc_now_iso(),
        }
        health = getattr(client, "gateway_health", None)
        if callable(health):
            try:
                gateway["health"] = health()
                gateway["reachable"] = True
            except Exception as exc:
                gateway["error"] = str(exc)
        metrics = getattr(getattr(client, "metrics", None), "snapshot", None)
        resolver_cache = getattr(resolver, "cache_snapshot", None)
        if not callable(resolver_cache):
            resolver_cache = getattr(getattr(resolver, "cache", None), "snapshot", None)
        return {
            "gateway": gateway,
            "resolver_cache": resolver_cache() if callable(resolver_cache) else {},
            "client_metrics": metrics() if callable(metrics) else {},
            "esi": self._public_esi_health(),
        }

    def _event_bootstrap_payload(
        self,
        active_items: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the compact state required by alert SSE consumers."""
        hostile_counts = _active_hostile_counts(alerts, active_items)
        systems = [
            {
                "name": system_name,
                "system_name": system_name,
                "hostile_count": hostile_count,
            }
            for system_name, hostile_count in sorted(hostile_counts.items())
        ]
        clients = self._store().heartbeat_snapshot()
        monitoring_nodes = _monitoring_target_state(clients)
        return {
            "schema_version": "intel_bootstrap.v1",
            "generated_at": utc_now_iso(),
            "map": {
                "systems": systems,
                "summary": {
                    "system_count": len(systems),
                    "alert_count": len(alerts),
                },
            },
            "alerts": alerts,
            "active_intel": active_items,
            "hostile_personnel": self._hostile_personnel_snapshot(active_items),
            "clients": clients,
            "monitoring_nodes": monitoring_nodes,
            "monitoring_nodes_version": _monitoring_nodes_version(monitoring_nodes),
        }

    def _hostile_personnel_snapshot(
        self,
        active_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return the current hostile roster using the map's server-side rules."""
        store = self._store()
        system_intel = store._aggregate_active_by_system(active_items)
        rosters: dict[str, dict[str, Any]] = {}
        for item in active_items:
            if not isinstance(item, dict) or not bool(item.get("active", True)):
                continue
            name = str(item.get("name") or "").strip()
            if not name or not store._active_item_is_hostile(item):
                continue
            source = str(item.get("source") or "").strip().casefold()
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            if source == "eve-sentry-detector":
                if metadata.get("presence_only"):
                    continue
                if (
                    metadata.get("identity_status") != "resolved"
                    or self._optional_positive_int(item.get("character_id")) is None
                ):
                    continue
            system_name = str(
                item.get("system_name") or item.get("system") or "Unknown"
            ).strip() or "Unknown"
            system_key = system_name.casefold()
            roster = rosters.setdefault(
                system_key,
                {
                    "system_name": system_name,
                    "system_id": item.get("system_id"),
                    "hostile_count": 0,
                    "personnel": {},
                },
            )
            character_id = self._optional_positive_int(item.get("character_id"))
            identity_key = (
                f"character:{character_id}"
                if character_id is not None
                else f"name:{name.casefold()}"
            )
            personnel = {
                "name": name,
                "character_id": character_id,
                "identity_status": str(metadata.get("identity_status") or "resolved"),
                "first_seen_at": str(item.get("first_seen_at") or ""),
            }
            existing = roster["personnel"].get(identity_key)
            if existing is None or (
                not existing.get("first_seen_at")
                and personnel["first_seen_at"]
            ):
                roster["personnel"][identity_key] = personnel

        result: list[dict[str, Any]] = []
        for system_key, roster in rosters.items():
            system_data = system_intel.get(system_key) or system_intel.get(
                roster["system_name"]
            )
            roster["hostile_count"] = int(
                (system_data or {}).get("hostile_count") or 0
            )
            roster["personnel"] = sorted(
                roster["personnel"].values(),
                key=lambda item: str(item.get("name") or "").casefold(),
            )
            result.append(roster)
        result.sort(key=lambda item: str(item.get("system_name") or "").casefold())
        return result

    def _map_snapshot_payload(self) -> dict[str, Any]:
        return self._map_snapshot_from_snapshot(
            self._runtime_snapshot(include_reports=False, include_alerts=False)
        )

    def _map_neighborhood_payload(
        self,
        *,
        system_names: list[str],
        system_ids: list[str],
        hops: int,
    ) -> dict[str, Any]:
        """Return only the configured map nodes within a few jumps of centers."""
        snapshot = self._map_snapshot_payload()
        systems = [
            item for item in snapshot.get("systems", []) if isinstance(item, dict)
        ]
        links = [
            item for item in snapshot.get("links", []) if isinstance(item, dict)
        ]
        max_hops = max(0, min(5, int(hops)))
        by_name = {
            str(item.get("name") or "").strip().casefold(): item
            for item in systems
            if str(item.get("name") or "").strip()
        }
        by_id = {
            str(item.get("system_id")): item
            for item in systems
            if self._optional_positive_int(item.get("system_id")) is not None
        }
        center_names: set[str] = set()
        for name in system_names:
            item = by_name.get(str(name or "").strip().casefold())
            if item is not None:
                center_names.add(str(item["name"]).strip())
        for system_id in system_ids:
            item = by_id.get(str(system_id or "").strip())
            if item is not None:
                center_names.add(str(item["name"]).strip())

        adjacency: dict[str, set[str]] = {
            str(item["name"]): set() for item in systems
        }
        for link in links:
            source = str(link.get("from") or "").strip()
            target = str(link.get("to") or "").strip()
            if source not in adjacency or target not in adjacency:
                continue
            adjacency[source].add(target)
            adjacency[target].add(source)

        included = set(center_names)
        frontier = set(center_names)
        for _ in range(max_hops):
            next_frontier = {
                neighbor
                for name in frontier
                for neighbor in adjacency.get(name, set())
                if neighbor not in included
            }
            included.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break

        return {
            "schema_version": "map_neighborhood.v1",
            "generated_at": snapshot.get("generated_at", ""),
            "hops": max_hops,
            "centers": sorted(center_names),
            "systems": [
                item for item in systems if str(item.get("name")) in included
            ],
            "links": [
                item
                for item in links
                if str(item.get("from") or "").strip() in included
                and str(item.get("to") or "").strip() in included
            ],
            "summary": {
                "system_count": len(included),
                "link_count": sum(
                    1
                    for item in links
                    if str(item.get("from") or "").strip() in included
                    and str(item.get("to") or "").strip() in included
                ),
            },
        }

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
        hostile_personnel = self._hostile_personnel_snapshot(active_items)
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
            "hostile_personnel": hostile_personnel,
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
        active_by_source_id: dict[str, dict[str, Any]] = {}
        for item in active_items:
            for source_id in self._active_item_source_ids(item):
                active_by_source_id[source_id] = item

        # A detector can publish one OCR report per character.  Keep the
        # report-level ``names`` field unchanged, but also expose the complete
        # current detector snapshot so integrations do not mistake one report
        # (or a locally truncated message) for the full hostile roster.
        detector_rosters: dict[tuple[str, str], dict[str, Any]] = {}
        for item in active_items:
            if not bool(item.get("active", True)):
                continue
            if str(item.get("source") or "").strip().casefold() != (
                "eve-sentry-detector"
            ):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or "hostile_icon_count" not in metadata:
                continue
            if not store._active_item_is_hostile(item):
                continue
            if (
                not metadata.get("presence_only")
                and (
                    metadata.get("identity_status") != "resolved"
                    or not item.get("character_id")
                )
            ):
                continue
            client_id = str(
                metadata.get("client_id")
                or item.get("source_instance")
                or "unknown"
            ).strip() or "unknown"
            system_name = str(
                item.get("system_name") or item.get("system") or "Unknown"
            ).strip() or "Unknown"
            roster = detector_rosters.setdefault(
                (client_id, system_name.casefold()),
                {"system_name": system_name, "names": {}, "character_ids": set()},
            )
            name = str(item.get("name") or "").strip()
            if name:
                roster["names"].setdefault(name.casefold(), name)
            character_id = item.get("character_id")
            try:
                if character_id is not None:
                    roster["character_ids"].add(int(character_id))
            except (TypeError, ValueError):
                pass

        for roster in detector_rosters.values():
            roster["active_names"] = [
                roster["names"][key] for key in sorted(roster["names"])
            ]
            roster["active_character_ids"] = sorted(roster["character_ids"])

        alerts = []
        report_items = reports if reports else store._reports_snapshot()
        for report in report_items:
            source_id = str(getattr(report, "report_id", "") or "")
            active_item = active_by_source_id.get(source_id)
            if active_item is None:
                continue
            alert = store._alert_from_report(report)
            if alert is None:
                continue
            alert = store._alert_to_dict(report, alert)
            if not self._active_alert_is_hostile(alert, active_item):
                continue
            data = dict(alert)
            data["active_intel_id"] = active_item.get("id")
            metadata = (
                active_item.get("metadata")
                if isinstance(active_item.get("metadata"), dict)
                else {}
            )
            if str(active_item.get("source") or "").strip().casefold() == (
                "eve-sentry-detector"
            ) and "hostile_icon_count" in metadata:
                detector_client_id = str(
                    metadata.get("client_id")
                    or active_item.get("source_instance")
                    or "unknown"
                )
                data["detector_client_id"] = detector_client_id
                roster = detector_rosters.get(
                    (
                        detector_client_id,
                        str(
                            active_item.get("system_name")
                            or active_item.get("system")
                            or "Unknown"
                        ).strip().casefold(),
                    )
                )
                if roster is not None:
                    data["active_names"] = list(roster["active_names"])
                    data["active_character_ids"] = list(
                        roster["active_character_ids"]
                    )
                try:
                    data["hostile_count"] = max(
                        0,
                        int(metadata.get("hostile_icon_count") or 0),
                    )
                except (TypeError, ValueError):
                    data["hostile_count"] = 0
                data["hostile_icon_seen_at"] = str(
                    metadata.get("hostile_icon_seen_at") or ""
                )
            alerts.append(data)

        alerts.sort(key=lambda alert: alert["created_at"], reverse=True)
        if limit is not None:
            alerts = alerts[:max(0, limit)]
        return alerts

    def _active_alert_is_hostile(
        self,
        alert: dict[str, Any],
        active_item: dict[str, Any],
    ) -> bool:
        metadata = (
            active_item.get("metadata")
            if isinstance(active_item.get("metadata"), dict)
            else {}
        )
        source = str(active_item.get("source") or "").strip().casefold()
        if source == "eve-sentry-detector" and metadata.get("presence_only"):
            try:
                return int(metadata.get("hostile_icon_count") or 0) > 0
            except (TypeError, ValueError):
                return False

        if source == "eve-sentry-detector" and (
            metadata.get("identity_status") != "resolved"
            or not active_item.get("character_id")
        ):
            return False

        classification = str(alert.get("classification") or "").strip().casefold()
        if classification == "white":
            return False
        if classification == "red":
            return True

        evidence = alert.get("evidence")
        if isinstance(evidence, list):
            evidence_types = {
                str(item.get("type") or "").strip().casefold()
                for item in evidence
                if isinstance(item, dict)
            }
            if any(item.startswith("friendly_") for item in evidence_types):
                return False
            if any(item.startswith("hostile_") for item in evidence_types):
                return True

        hostile_count = metadata.get("hostile_count")
        if isinstance(hostile_count, int) and hostile_count > 0:
            return True

        return source in {"channel", "intel_channel", "intel_channel_report"}

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
            cursor_values = query.get("cursor")
            if cursor_values:
                cursor = str(cursor_values[0] or "").strip()
                page = self._store().report_page(
                    cursor="" if cursor == "start" else cursor,
                    system=query.get("system", [""])[0],
                    name=query.get("name", [""])[0],
                    limit=limit or 100,
                )
                reports = page["reports"]
                next_cursor = str(page.get("next_cursor") or "")
                self._send_json(
                    {
                        "reports": reports,
                        "count": len(reports),
                        "next_cursor": next_cursor or None,
                        "has_more": bool(next_cursor),
                    }
                )
                return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        reports = self._store().list_reports(
            system=query.get("system", [""])[0],
            name=query.get("name", [""])[0],
            limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
        )
        self._send_json({"reports": reports, "count": len(reports)})

    def _send_observation_list(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        try:
            limit = self._parse_optional_int(query.get("limit", [""])[0])
            cursor_values = query.get("cursor")
            if cursor_values:
                cursor = str(cursor_values[0] or "").strip()
                page = self._store().observation_page(
                    cursor="" if cursor == "start" else cursor,
                    source=query.get("source", [""])[0],
                    system=query.get("system", [""])[0],
                    name=query.get("name", [""])[0],
                    limit=limit or 100,
                )
                observations = page["observations"]
                next_cursor = str(page.get("next_cursor") or "")
                self._send_json(
                    {
                        "observations": observations,
                        "count": len(observations),
                        "next_cursor": next_cursor or None,
                        "has_more": bool(next_cursor),
                    }
                )
                return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        observations = self._store().list_observations(
            source=query.get("source", [""])[0],
            system=query.get("system", [""])[0],
            name=query.get("name", [""])[0],
            limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
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
            limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
        )
        active = self._visible_active_items(self._store(), active)
        self._send_json(
            {
                "active_intel": active,
                "count": len(active),
                "generated_at": utc_now_iso(),
            }
        )

    def _send_alert_history(self, raw_query: str = "") -> None:
        query = parse_qs(raw_query)
        try:
            limit = self._parse_optional_int(query.get("limit", [""])[0])
            filters = self._parse_alert_filters(query)
            store = self._store()
            list_history = getattr(store, "list_alert_history", None)
            if not callable(list_history):
                list_history = store.list_alerts
            alerts = list_history(
                since=query.get("since", [""])[0],
                limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
                **filters,
            )
            alerts = [alert for alert in alerts if _is_hostile_history_alert(alert)]
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "schema_version": "alert_history.v1",
                "alerts": alerts,
                "count": len(alerts),
            }
        )

    def _send_hostile_waves(self, raw_query: str = "") -> None:
        query = parse_qs(raw_query)
        try:
            limit = self._parse_optional_int(query.get("limit", [""])[0])
            waves = self._store().list_hostile_waves(
                since=query.get("since", [""])[0],
                limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(
            {
                "schema_version": "hostile_waves.v1",
                "waves": waves,
                "count": len(waves),
                "generated_at": utc_now_iso(),
            }
        )

    def _send_hostile_systems(self) -> None:
        """Return a stable, minimal hostile-system feed for integrations."""
        store = self._store()
        active_items = self._visible_active_items(store, store.list_active_intel())
        alerts = self._active_alert_list(limit=None, active_items=active_items)
        systems = sorted(
            _active_hostile_counts(alerts, active_items),
            key=str.casefold,
        )
        self._send_json(
            {
                "schema_version": "hostile_systems.v1",
                "generated_at": utc_now_iso(),
                "count": len(systems),
                "systems": systems,
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
                    limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
                    **filters,
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
        else:
            alerts = self._store().list_alerts(
                since=query.get("since", [""])[0],
                limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
                **filters,
            )
        self._send_json({"alerts": alerts, "count": len(alerts)})

    def _active_alert_list(
        self,
        since: str | None = None,
        limit: int | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
        include_since: bool = False,
        active_items: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        store = self._store()
        if active_items is None:
            active_items = self._visible_active_items(store, store.list_active_intel())
        alerts = self._active_alerts_from_reports(
            store,
            [],
            active_items,
            limit=None,
        )

        return self._filter_active_alerts(
            store,
            alerts,
            since=since,
            limit=limit,
            min_score=min_score,
            min_level=min_level,
            include_since=include_since,
        )

    def _filter_active_alerts(
        self,
        store: IntelStore,
        alerts: list[dict[str, Any]],
        *,
        since: str | None = None,
        limit: int | None = None,
        min_score: int | None = None,
        min_level: str | None = None,
        include_since: bool = False,
    ) -> list[dict[str, Any]]:
        """Apply stream/query filters without rebuilding active alerts."""
        filtered = list(alerts)

        since_query = since.strip() if since else ""
        if since_query:
            if include_since:
                filtered = [
                    alert for alert in filtered
                    if alert["created_at"] >= since_query
                ]
            else:
                filtered = [
                    alert for alert in filtered
                    if alert["created_at"] > since_query
                ]

        min_score_value = store._optional_score(min_score)
        min_level_rank = store._alert_level_rank(min_level)
        filtered = [
            alert for alert in filtered
            if store._alert_passes_filters(
                alert,
                acknowledged=None,
                min_score=min_score_value,
                min_level_rank=min_level_rank,
            )
        ]
        if limit is not None:
            filtered = filtered[:max(0, limit)]
        return filtered

    def _cached_active_event_state(
        self,
        store: IntelStore,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the active SSE state once per change generation and second."""
        cache = getattr(store, "_sse_active_event_cache", None)
        if cache is None:
            with _ACTIVE_EVENT_SNAPSHOT_INIT_LOCK:
                cache = getattr(store, "_sse_active_event_cache", None)
                if cache is None:
                    cache = {
                        "lock": threading.RLock(),
                        "generation": -1,
                        "created_at": 0.0,
                        "state": None,
                    }
                    store._sse_active_event_cache = cache

        now = time.monotonic()
        generation = _event_stream_generation()
        with cache["lock"]:
            if (
                cache["state"] is not None
                and cache["generation"] == generation
                and now - float(cache["created_at"]) < _ACTIVE_EVENT_SNAPSHOT_TTL_SECONDS
            ):
                state = cache["state"]
            else:
                active_items = self._visible_active_items(
                    store,
                    store.list_active_intel(),
                )
                alerts = self._active_alert_list(
                    since="",
                    limit=None,
                    active_items=active_items,
                )
                presence_alerts = self._active_presence_alerts(active_items)
                state = (active_items, alerts, presence_alerts)
                cache["generation"] = generation
                cache["created_at"] = now
                cache["state"] = state
            return copy.deepcopy(state)

    def _active_presence_alerts(
        self,
        active_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expose detector-only hostile presence as one resumable alert event."""
        alerts: list[dict[str, Any]] = []
        for item in active_items:
            if not isinstance(item, dict) or not bool(item.get("active", True)):
                continue
            if str(item.get("source") or "").strip().casefold() != (
                "eve-sentry-detector"
            ):
                continue
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or not metadata.get("presence_only"):
                continue
            try:
                hostile_count = max(0, int(metadata.get("hostile_icon_count") or 0))
            except (TypeError, ValueError):
                hostile_count = 0
            if hostile_count <= 0:
                continue
            active_id = str(item.get("id") or "").strip()
            if not active_id:
                continue
            system_name = str(
                item.get("system_name") or item.get("system") or "Unknown"
            ).strip() or "Unknown"
            first_seen_at = str(
                item.get("first_seen_at")
                or metadata.get("hostile_icon_seen_at")
                or item.get("last_seen_at")
                or utc_now_iso()
            ).strip()
            detector_client_id = str(
                metadata.get("client_id") or item.get("source_instance") or "unknown"
            ).strip() or "unknown"
            event_id = "presence_" + hashlib.sha256(
                active_id.encode("utf-8")
            ).hexdigest()[:24]
            alerts.append(
                {
                    "id": event_id,
                    "level": "critical",
                    "score": 100,
                    "system_name": system_name,
                    "system": system_name,
                    "system_id": item.get("system_id"),
                    "names": [],
                    "character_ids": [],
                    "classification": "red",
                    "hostile_count": hostile_count,
                    "active_names": [],
                    "active_character_ids": [],
                    "created_at": first_seen_at,
                    "seen_at": str(
                        metadata.get("hostile_icon_seen_at")
                        or item.get("last_seen_at")
                        or first_seen_at
                    ),
                    "source_observation_id": active_id,
                    "verified_characters": [],
                    "evidence": [
                        {
                            "type": "hostile_icon",
                            "count": hostile_count,
                            "source": "detector",
                        }
                    ],
                    "presence_only": True,
                    "detector_client_id": detector_client_id,
                }
            )
        alerts.sort(key=lambda alert: str(alert.get("created_at") or ""))
        return alerts

    def _store(self) -> IntelStore:
        return self.server.store  # type: ignore[attr-defined,no-any-return]

    def _config_store(self) -> Any | None:
        return self.server.config_store  # type: ignore[attr-defined,no-any-return]

    def _invalidate_identity_cache(self) -> None:
        """Drop identity profiles after watchlist/standing rules change."""
        resolver = getattr(self._store(), "_resolver", None)
        cache = getattr(resolver, "cache", None)
        invalidate = getattr(cache, "invalidate_namespace", None)
        if not callable(invalidate):
            return
        for namespace in ("character", "corporation", "alliance"):
            invalidate(namespace)

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
            "esi": self._public_esi_health(),
            "killboard": self._killboard_health(store),
            "clients": store.heartbeat_summary(),
            "events": self._event_health(store),
        }

    def _readiness_payload(self) -> dict[str, Any]:
        storage_ready = self._storage_ready(self._store())
        return {
            "ok": storage_ready,
            "checks": {
                "storage": {"ok": storage_ready},
            },
        }

    def _storage_ready(self, store: IntelStore) -> bool:
        connect = getattr(store, "_connect", None)
        if callable(connect):
            try:
                with connect() as connection:
                    row = connection.execute("SELECT 1").fetchone()
                return row is not None
            except Exception as exc:
                logger.warning(
                    "Storage readiness probe failed (%s)",
                    type(exc).__name__,
                )
                return False

        path = getattr(store, "_filepath", None)
        return path is not None and self._storage_path_writable(path)

    def _storage_health(self, store: IntelStore) -> dict[str, Any]:
        postgres_dsn = getattr(store, "_postgres_safe_dsn", "")
        if postgres_dsn:
            return {
                "type": type(store).__name__,
                "writable": True,
            }
        path = getattr(store, "_db_path", None) or getattr(store, "_filepath", None)
        return {
            "type": type(store).__name__,
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
            "schema_version": config.get("schema_version", ""),
            "source": config.get("source", ""),
            "layout_mode": config.get("layout_mode", ""),
            "system_count": active_system_count,
            "link_count": active_link_count,
            "last_refreshed_at": config.get("last_refreshed_at", ""),
            "refresh_error": bool(config.get("last_refresh_error")),
        }

    def _event_health(self, store: IntelStore) -> dict[str, Any]:
        _ = store
        return {
            "alert_query_ok": True,
            "sse": {
                "enabled": True,
                "path": "/api/v1/events",
                "legacy_path": "/api/events",
                "active_connections": _active_event_stream_count(),
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
        return self._store().alert_for_observation(observation_id)

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
        raw = self._read_json_body()
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise RequestBodyError("request body must be valid UTF-8") from exc
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _read_optional_json(self) -> dict[str, Any]:
        raw = self._read_json_body()
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise RequestBodyError("request body must be valid UTF-8") from exc
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _read_json_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestBodyError("Content-Length header is required")
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise RequestBodyError("Content-Length must be a non-negative integer")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise RequestBodyError(
                "Content-Length must be a non-negative integer"
            ) from exc
        if length < 0:
            raise RequestBodyError("Content-Length must be a non-negative integer")
        if length > MAX_JSON_BODY_BYTES:
            raise RequestBodyError(
                f"request body must not exceed {MAX_JSON_BODY_BYTES} bytes",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        return self.rfile.read(length)

    def _parse_optional_int(self, raw: str) -> int | None:
        raw = raw.strip()
        if not raw:
            return None
        value = int(raw)
        if value < 0:
            raise ValueError("limit must be non-negative")
        if value > MAX_QUERY_LIMIT:
            raise ValueError(f"limit must not exceed {MAX_QUERY_LIMIT}")
        return value

    def _parse_optional_float_param(self, raw: str, label: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
        if value < 0:
            raise ValueError(f"{label} must be non-negative")
        maximum = {
            "timeout": MAX_SSE_TIMEOUT_SECONDS,
            "heartbeat": MAX_SSE_HEARTBEAT_SECONDS,
        }.get(label)
        if maximum is not None and value > maximum:
            raise ValueError(f"{label} must not exceed {maximum:g}")
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
            limit=DEFAULT_QUERY_LIMIT if limit is None else limit,
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

    def _event_stream_cursor(
        self,
        since: str,
    ) -> tuple[str, str, bool, tuple[int, str] | None]:
        """Resolve explicit since or browser Last-Event-ID into a stream cursor."""
        since = str(since or "").strip()
        last_event_id = str(self.headers.get("Last-Event-ID") or "").strip()
        if last_event_id:
            store = self._store()
            stream_cursor = store.resolve_alert_stream_cursor(last_event_id)
            if stream_cursor is not None:
                return (
                    store.alert_cursor(last_event_id),
                    last_event_id,
                    True,
                    stream_cursor,
                )

            if "T" in last_event_id and last_event_id[:1].isdigit():
                return last_event_id, "", False, None

        if since:
            return since, "", False, None
        return "", "", False, None

    @_track_event_stream
    def _stream_events(
        self,
        since: str = "",
        resume_after_id: str = "",
        stream_cursor: tuple[int, str] | None = None,
        include_since: bool = False,
        limit: int = 50,
        timeout_seconds: float | None = None,
        heartbeat_seconds: float = 15.0,
        min_score: int | None = None,
        min_level: str | None = None,
        active_only: bool = False,
        include_bootstrap: bool = True,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-transform")
        # Disable reverse-proxy buffering even when a deployment uses the
        # generic /api/ location instead of the dedicated SSE location.
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        # Flush a byte immediately so clients can distinguish a live stream
        # from a handler blocked while building the first active snapshot.
        try:
            self._write_sse_comment("connected")
        except (BrokenPipeError, ConnectionResetError):
            return

        last_seen = since.strip()
        resume_after_id = resume_after_id.strip()
        stream_event_id = resume_after_id or last_seen
        sent_ids: set[str] = set()
        last_bootstrap_fingerprint = ""
        last_monitoring_target_state: list[dict[str, Any]] | None = None
        active_hostile_counts: dict[str, int] | None = None
        deadline = (
            time.monotonic() + max(0.0, timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        heartbeat_interval = max(0.0, heartbeat_seconds)
        next_heartbeat_at = (
            time.monotonic() + heartbeat_interval if heartbeat_interval else 0.0
        )
        auth_service = self._auth_service()
        last_auth_generation: int | None = None
        next_auth_check_at = 0.0
        while True:
            now = time.monotonic()
            auth_generation = (
                int(auth_service.authorization_generation)
                if auth_service is not None
                and hasattr(auth_service, "authorization_generation")
                else 0
            )
            if (
                last_auth_generation is None
                or auth_generation != last_auth_generation
                or now >= next_auth_check_at
            ):
                if not self._stream_principal_active():
                    return
                last_auth_generation = auth_generation
                next_auth_check_at = now + SSE_AUTH_RECHECK_SECONDS
            next_client_stale_in: float | None = None
            try:
                event_generation = _event_stream_generation()
                wrote_event = False
                current_include_since = bool(last_seen) and (
                    include_since or bool(sent_ids)
                )
                active_items: list[dict[str, Any]] | None = None
                alert_cursors: dict[str, tuple[int, str]] = {}
                active_presence_alerts: list[dict[str, Any]] = []
                if active_only:
                    store = self._store()
                    (
                        active_items,
                        active_alerts,
                        active_presence_alerts,
                    ) = self._cached_active_event_state(store)
                    alerts = self._filter_active_alerts(
                        store,
                        active_alerts,
                        since="" if stream_cursor is not None else last_seen,
                        limit=None,
                        include_since=current_include_since,
                        min_score=min_score,
                        min_level=min_level,
                    )
                    cursor_alerts: list[
                        tuple[tuple[int, str], dict[str, Any]]
                    ] = []
                    for alert in alerts:
                        alert_id = str(alert.get("id") or "")
                        cursor = store.resolve_alert_stream_cursor(alert_id)
                        if cursor is None:
                            continue
                        if stream_cursor is not None and cursor <= stream_cursor:
                            continue
                        alert_cursors[alert_id] = cursor
                        cursor_alerts.append((cursor, alert))
                    cursor_alerts.sort(key=lambda item: item[0])
                    if stream_cursor is None and not last_seen and limit > 0:
                        cursor_alerts = cursor_alerts[-limit:]
                    ordered_alerts = [alert for _, alert in cursor_alerts]
                    alerts = list(ordered_alerts)
                else:
                    stream_page = self._store().list_alert_stream_page(
                        after=stream_cursor,
                        since="" if stream_cursor is not None else last_seen,
                        include_since=current_include_since,
                        limit=limit,
                        min_score=min_score,
                        min_level=min_level,
                    )
                    ordered_alerts = [alert for _, alert in stream_page]
                    alert_cursors = {
                        str(alert.get("id") or ""): cursor
                        for cursor, alert in stream_page
                    }
                    alerts = list(ordered_alerts)
                active_snapshot_alerts: list[dict[str, Any]] = []
                if active_only:
                    active_snapshot_alerts = list(active_alerts)
                    active_snapshot_alerts.extend(active_presence_alerts)
                    active_snapshot_alerts.sort(
                        key=lambda item: str(item.get("created_at") or ""),
                        reverse=True,
                    )
                    current_hostile_counts = _active_hostile_counts(
                        active_snapshot_alerts,
                        active_items,
                    )
                    if active_hostile_counts is not None:
                        for system_name in sorted(
                            set(active_hostile_counts) - set(current_hostile_counts)
                        ):
                            safe_at = utc_now_iso()
                            self._write_sse(
                                "safe",
                                safe_at,
                                {
                                    "system_name": system_name,
                                    "system": system_name,
                                    "hostile_count": 0,
                                    "active": False,
                                    "created_at": safe_at,
                                    "message": f"✅ {system_name} 清空",
                                },
                            )
                            wrote_event = True
                    active_hostile_counts = current_hostile_counts
                if active_only and include_bootstrap:
                    bootstrap = self._event_bootstrap_payload(
                        active_items or [],
                        active_snapshot_alerts,
                    )
                    current_monitoring_target_state = _monitoring_target_state(
                        bootstrap.get("clients")
                    )
                    monitoring_node_changes = (
                        _monitoring_node_changes(
                            last_monitoring_target_state,
                            current_monitoring_target_state,
                        )
                        if last_monitoring_target_state is not None
                        else []
                    )
                    last_monitoring_target_state = current_monitoring_target_state
                    next_client_stale_in = _next_monitoring_heartbeat_stale_in(
                        bootstrap.get("clients")
                    )
                    fingerprint = self._bootstrap_event_fingerprint(bootstrap)
                    if fingerprint != last_bootstrap_fingerprint:
                        # Keep the browser's Last-Event-ID on a resumable alert
                        # cursor even when bootstrap is the last event emitted.
                        bootstrap_event_id = stream_event_id
                        if not bootstrap_event_id and ordered_alerts:
                            bootstrap_event_id = str(
                                ordered_alerts[-1].get("id") or ""
                            ).strip()
                        if not bootstrap_event_id:
                            bootstrap_event_id = str(
                                bootstrap.get("generated_at") or last_seen or ""
                            ).strip()
                        bootstrap["monitoring_node_changes"] = monitoring_node_changes
                        if monitoring_node_changes:
                            self._write_sse(
                                "monitoring_node",
                                bootstrap_event_id,
                                {
                                    "schema_version": "monitoring_node_event.v1",
                                    "generated_at": str(
                                        bootstrap.get("generated_at") or utc_now_iso()
                                    ),
                                    "changes": monitoring_node_changes,
                                    "nodes": current_monitoring_target_state,
                                    "nodes_version": _monitoring_nodes_version(
                                        current_monitoring_target_state
                                    ),
                                },
                            )
                        self._write_sse("bootstrap", bootstrap_event_id, bootstrap)
                        last_bootstrap_fingerprint = fingerprint
                        wrote_event = True
                if active_only:
                    presence_alerts = list(active_presence_alerts)
                    if last_seen:
                        if current_include_since:
                            presence_alerts = [
                                alert
                                for alert in presence_alerts
                                if str(alert.get("created_at") or "") >= last_seen
                            ]
                        else:
                            presence_alerts = [
                                alert
                                for alert in presence_alerts
                                if str(alert.get("created_at") or "") > last_seen
                            ]
                    presence_alerts.sort(
                        key=lambda item: str(item.get("created_at") or "")
                    )
                    alerts.extend(presence_alerts)
                ordered_alerts = alerts
                emitted_alert_count = 0
                for alert in ordered_alerts:
                    alert_id = str(alert.get("id") or "")
                    if not alert_id or alert_id in sent_ids:
                        continue
                    alert_cursor = alert_cursors.get(alert_id)
                    if alert_cursor is not None and emitted_alert_count >= limit:
                        continue
                    self._write_sse("alert", alert_id, alert)
                    wrote_event = True
                    if alert_cursor is not None:
                        emitted_alert_count += 1
                    sent_ids.add(alert_id)
                    stream_event_id = alert_id
                    if alert_cursor is not None and (
                        stream_cursor is None or alert_cursor > stream_cursor
                    ):
                        stream_cursor = alert_cursor
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
            remaining = deadline - now if deadline is not None else None
            if remaining is not None and remaining <= 0:
                break
            sleep_for = min(1.0, remaining) if remaining is not None else 1.0
            if heartbeat_interval:
                sleep_for = min(sleep_for, max(0.0, next_heartbeat_at - now))
            if next_client_stale_in is not None:
                sleep_for = min(sleep_for, next_client_stale_in)
            _wait_for_event_stream_change(event_generation, sleep_for)

    def _bootstrap_event_fingerprint(self, payload: dict[str, Any]) -> str:
        stable_payload = self._without_generated_at(
            {
                "active_intel": payload.get("active_intel"),
                "alerts": payload.get("alerts"),
                "hostile_personnel": payload.get("hostile_personnel"),
                "monitoring_targets": _monitoring_target_state(
                    payload.get("clients")
                ),
            }
        )
        encoded = json.dumps(
            stable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def _without_generated_at(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._without_generated_at(item)
                for key, item in value.items()
                if key
                not in {
                    "generated_at",
                    "identity_checked_at",
                    "last_seen_at",
                }
            }
        if isinstance(value, list):
            return [self._without_generated_at(item) for item in value]
        return value

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
