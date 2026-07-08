"""Read-only integration status check for live EVE Sentry clients."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.intel_client import IntelApiClient, IntelApiError

SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "character_owner_hash",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "token",
    "token_file",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check a live EVE Sentry server and client heartbeats without "
            "creating reports, alerts, observations, or chatlog data."
        )
    )
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--output",
        type=Path,
        help="write the full JSON status payload to this evidence file",
    )
    parser.add_argument(
        "--expect-detector",
        action="store_true",
        help="fail unless at least one detector_client heartbeat is online",
    )
    parser.add_argument(
        "--expect-alert-client",
        action="store_true",
        help="fail unless at least one alert_client heartbeat is online",
    )
    parser.add_argument(
        "--expect-alert-mode",
        choices=["events", "poll"],
        default="",
        help="fail unless an online alert_client heartbeat reports this transport mode",
    )
    parser.add_argument(
        "--expect-alert-popup",
        action="store_true",
        help="fail unless an online alert_client heartbeat reports popup=true",
    )
    parser.add_argument(
        "--expect-alert-details",
        action="store_true",
        help="fail unless an online alert_client heartbeat reports details=true",
    )
    parser.add_argument(
        "--expect-alert-healthy",
        action="store_true",
        help="fail unless an online alert_client heartbeat has last_success_at and no last_error",
    )
    parser.add_argument(
        "--expect-channel-client",
        action="store_true",
        help="fail unless at least one standalone channel_client heartbeat is online",
    )
    parser.add_argument(
        "--expect-monitoring",
        action="store_true",
        help="fail unless a detector client reports running/monitoring state",
    )
    parser.add_argument(
        "--expect-channel-monitoring",
        action="store_true",
        help=(
            "fail unless an online detector client reports selected-channel "
            "Chatlogs monitoring"
        ),
    )
    parser.add_argument(
        "--min-targets",
        type=int,
        default=0,
        help="fail unless detector heartbeat details include at least this many targets",
    )
    parser.add_argument(
        "--min-active-ocr-targets",
        type=int,
        default=0,
        help=(
            "fail unless /api/v1/active-intel contains OCR rows from at least "
            "this many distinct detector targets"
        ),
    )
    parser.add_argument(
        "--require-event-health",
        action="store_true",
        help="fail unless /api/health reports event queries are healthy",
    )
    parser.add_argument(
        "--require-active-intel",
        action="store_true",
        help="fail unless /api/v1/active-intel currently returns at least one active row",
    )
    parser.add_argument(
        "--check-esi",
        action="store_true",
        help="read /api/v1/esi/status and include it in the status checks",
    )
    parser.add_argument(
        "--check-map",
        action="store_true",
        help="read /api/v1/map and include map snapshot availability in the checks",
    )
    parser.add_argument(
        "--check-events-stream",
        action="store_true",
        help="connect to /api/v1/events briefly; no alert is required",
    )
    parser.add_argument(
        "--check-alert-detail",
        action="store_true",
        help="read detail for the newest real alert when one exists; skips when none exist",
    )
    parser.add_argument(
        "--limit-alerts",
        type=int,
        default=5,
        help="read this many recent alerts for visibility; does not create alerts",
    )
    return parser.parse_args(argv)


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        raise RuntimeError(f"GET {url} timed out after {timeout}s") from exc
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"GET {url} returned a non-object JSON payload")
    return payload


def base_url(server: str) -> str:
    return str(server or "").rstrip("/")


def health_url(server: str) -> str:
    return f"{base_url(server)}/api/health"


def event_stream_url(server: str) -> str:
    params = urlencode({"timeout": "0.1", "heartbeat": "0.05", "limit": "1"})
    return f"{base_url(server)}/api/v1/events?{params}"


def probe_event_stream(server: str, timeout: float) -> dict[str, Any]:
    url = event_stream_url(server)
    request = Request(url, headers={"Accept": "text/event-stream"})
    try:
        with urlopen(request, timeout=max(timeout, 1.0)) as response:
            content_type = response.headers.get("Content-Type", "")
            try:
                sample = response.readline().decode("utf-8", errors="replace")
            except TimeoutError:
                sample = ""
    except TimeoutError as exc:
        raise RuntimeError(f"GET {url} timed out after {max(timeout, 1.0)}s") from exc
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc
    return {
        "content_type": content_type,
        "sample": sample,
        "ok": content_type.lower().startswith("text/event-stream"),
    }


def endpoint_record(url: str, ok: bool, detail: str = "", count: int | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "method": "GET",
        "url": url,
        "ok": bool(ok),
        "detail": detail,
    }
    if count is not None:
        record["count"] = count
    return record


def alert_detail_id(alerts: list[dict[str, Any]]) -> str:
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        alert_id = str(alert.get("id") or "").strip()
        if alert_id:
            return alert_id
    return ""


def classify_clients(clients: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    heartbeats = clients.get("heartbeats", [])
    if not isinstance(heartbeats, list):
        heartbeats = []
    result: dict[str, list[dict[str, Any]]] = {}
    for item in heartbeats:
        if not isinstance(item, dict):
            continue
        client_type = str(item.get("client_type") or "unknown")
        result.setdefault(client_type, []).append(item)
    return result


def is_online(item: dict[str, Any]) -> bool:
    if isinstance(item.get("online"), bool):
        return bool(item.get("online"))
    age_seconds = item.get("age_seconds")
    stale_after = item.get("stale_after_seconds")
    try:
        if age_seconds is not None and stale_after is not None:
            return float(age_seconds) <= float(stale_after)
    except (TypeError, ValueError):
        return False
    status = str(item.get("status") or "").strip().lower()
    return bool(status) and status not in {"offline", "stale"}


def detector_target_count(detectors: list[dict[str, Any]]) -> int:
    highest = 0
    for item in detectors:
        details = item.get("details", {})
        if not isinstance(details, dict):
            continue
        targets = details.get("targets", [])
        target_count = details.get("target_count")
        if isinstance(target_count, int):
            highest = max(highest, target_count)
        if isinstance(targets, list):
            highest = max(highest, len(targets))
    return highest


def detector_is_monitoring(detectors: list[dict[str, Any]]) -> bool:
    for item in detectors:
        if str(item.get("status") or "").strip().lower() == "running":
            return True
        details = item.get("details", {})
        if isinstance(details, dict):
            if details.get("monitoring") is True:
                return True
            targets = details.get("targets", [])
            if isinstance(targets, list) and any(
                isinstance(target, dict) and target.get("monitoring") is True
                for target in targets
            ):
                return True
    return False


def detector_channel_is_monitoring(detectors: list[dict[str, Any]]) -> bool:
    for item in detectors:
        details = item.get("details", {})
        if isinstance(details, dict) and details.get("channel_monitoring") is True:
            channels = details.get("channels", [])
            return not isinstance(channels, list) or bool(channels)
    return False


def detector_channel_count(detectors: list[dict[str, Any]]) -> int:
    highest = 0
    for item in detectors:
        details = item.get("details", {})
        if not isinstance(details, dict) or details.get("channel_monitoring") is not True:
            continue
        channels = details.get("channels", [])
        if isinstance(channels, list):
            highest = max(highest, len(channels))
        else:
            highest = max(highest, 1)
    return highest


def heartbeat_details(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details", {})
    return details if isinstance(details, dict) else {}


def alert_client_modes(alert_clients: list[dict[str, Any]]) -> list[str]:
    modes = []
    for item in alert_clients:
        details = heartbeat_details(item)
        for key in ("mode", "transport"):
            mode = str(details.get(key) or "").strip()
            if mode and mode not in modes:
                modes.append(mode)
    return modes


def alert_client_has_mode(alert_clients: list[dict[str, Any]], mode: str) -> bool:
    expected = str(mode or "").strip().casefold()
    if not expected:
        return True
    for item in alert_clients:
        details = heartbeat_details(item)
        values = {
            str(details.get("mode") or "").strip().casefold(),
            str(details.get("transport") or "").strip().casefold(),
        }
        if expected in values:
            return True
    return False


def alert_client_flag_enabled(alert_clients: list[dict[str, Any]], field: str) -> bool:
    return any(heartbeat_details(item).get(field) is True for item in alert_clients)


def alert_client_is_healthy(alert_clients: list[dict[str, Any]]) -> bool:
    for item in alert_clients:
        details = heartbeat_details(item)
        last_error = str(details.get("last_error") or "").strip()
        last_success_at = str(details.get("last_success_at") or "").strip()
        if not last_error and last_success_at:
            return True
    return False


def compact_heartbeat(item: dict[str, Any]) -> dict[str, Any]:
    details = heartbeat_details(item)
    return {
        "client_id": item.get("client_id", ""),
        "client_type": item.get("client_type", ""),
        "label": item.get("label", ""),
        "status": item.get("status", ""),
        "online": is_online(item),
        "seen_at": item.get("seen_at", ""),
        "age_seconds": item.get("age_seconds"),
        "stale_after_seconds": item.get("stale_after_seconds"),
        "heartbeat_interval_seconds": item.get("heartbeat_interval_seconds"),
        "details": details,
    }


def active_source_rows(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("source") or "") == source
    ]


def active_ocr_target_key(row: dict[str, Any]) -> str:
    metadata = row.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    client_id = str(metadata.get("client_id") or "").strip()
    if client_id:
        return f"client:{client_id}"
    source_instance = str(
        row.get("source_instance") or metadata.get("source_instance") or ""
    ).strip()
    if source_instance:
        return f"source:{source_instance}"
    system_name = str(row.get("system_name") or "").strip()
    return f"system:{system_name}" if system_name else "unknown"


def active_ocr_target_count(rows: list[dict[str, Any]]) -> int:
    return len({active_ocr_target_key(row) for row in rows if isinstance(row, dict)})


def detector_channel_states(detectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    states = []
    for item in detectors:
        details = item.get("details", {})
        if not isinstance(details, dict):
            continue
        states.append(
            {
                "client_id": item.get("client_id", ""),
                "channel_monitoring": bool(details.get("channel_monitoring")),
                "channels": details.get("channels", []),
                "channel_last_action": details.get("channel_last_action", ""),
                "channel_last_error": details.get("channel_last_error", ""),
                "channel_last_success_at": details.get("channel_last_success_at", ""),
            }
        )
    return states


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expected_conditions(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "detector_client": bool(args.expect_detector),
        "alert_client": bool(args.expect_alert_client),
        "alert_mode": str(args.expect_alert_mode or ""),
        "alert_popup": bool(args.expect_alert_popup),
        "alert_details": bool(args.expect_alert_details),
        "alert_healthy": bool(args.expect_alert_healthy),
        "channel_client": bool(args.expect_channel_client),
        "detector_monitoring": bool(args.expect_monitoring),
        "detector_channel_monitoring": bool(args.expect_channel_monitoring),
        "min_targets": max(0, int(args.min_targets)),
        "min_active_ocr_targets": max(0, int(args.min_active_ocr_targets)),
        "event_health": bool(args.require_event_health),
        "active_intel": bool(args.require_active_intel),
        "esi_status": bool(args.check_esi),
        "map_snapshot": bool(args.check_map),
        "events_stream": bool(args.check_events_stream),
        "alert_detail": bool(args.check_alert_detail),
    }


def checked_urls(args: argparse.Namespace) -> list[str]:
    server = base_url(args.server)
    urls = [
        f"{server}/api/health",
        f"{server}/api/v1/clients",
        f"{server}/api/v1/alerts?{urlencode({'limit': str(max(0, int(args.limit_alerts)))})}",
        f"{server}/api/v1/active-intel?{urlencode({'limit': '50'})}",
    ]
    if args.check_esi:
        urls.append(f"{server}/api/v1/esi/status")
    if args.check_map:
        urls.append(f"{server}/api/v1/map")
    if args.check_events_stream:
        urls.append(event_stream_url(server))
    return urls


def write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def is_sensitive_key(key: str) -> bool:
    normalized = str(key or "").strip().casefold()
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or "authorization" in normalized
        or "cookie" in normalized
        or "password" in normalized
    )


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def build_status(args: argparse.Namespace) -> dict[str, Any]:
    api = IntelApiClient(args.server, timeout=max(0.1, float(args.timeout)))
    checks: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []

    health_payload = fetch_json(health_url(args.server), timeout=args.timeout)
    health = health_payload.get("health", health_payload)
    if not isinstance(health, dict):
        health = {}
    endpoints.append(
        endpoint_record(
            health_url(args.server),
            health.get("ok") is True,
            str(health.get("schema_version") or ""),
        )
    )
    add_check(
        checks,
        "health",
        health.get("ok") is True,
        str(health.get("schema_version") or ""),
    )
    if args.require_event_health:
        events = health.get("events", {})
        event_ok = isinstance(events, dict) and events.get("alert_query_ok") is True
        add_check(checks, "event_health", event_ok, json.dumps(events, ensure_ascii=False))
    if args.check_events_stream:
        stream = probe_event_stream(args.server, timeout=args.timeout)
        endpoints.append(
            endpoint_record(
                event_stream_url(args.server),
                bool(stream.get("ok")),
                str(stream.get("content_type") or ""),
            )
        )
        add_check(
            checks,
            "events_stream",
            bool(stream.get("ok")),
            str(stream.get("content_type") or ""),
        )

    clients = api.client_status()
    heartbeats = clients.get("heartbeats", [])
    if not isinstance(heartbeats, list):
        heartbeats = []
    endpoints.append(
        endpoint_record(
            f"{base_url(args.server)}/api/v1/clients",
            isinstance(heartbeats, list),
            "client heartbeat snapshot",
            count=len(heartbeats),
        )
    )
    grouped = classify_clients(clients)
    detectors = grouped.get("detector_client", [])
    alert_clients = grouped.get("alert_client", [])
    channel_clients = grouped.get("channel_client", [])
    online_detectors = [item for item in detectors if is_online(item)]
    online_alert_clients = [item for item in alert_clients if is_online(item)]
    online_channel_clients = [item for item in channel_clients if is_online(item)]
    alert_modes = alert_client_modes(online_alert_clients)
    alert_popup_enabled = alert_client_flag_enabled(online_alert_clients, "popup")
    alert_details_enabled = alert_client_flag_enabled(online_alert_clients, "details")
    alert_healthy = alert_client_is_healthy(online_alert_clients)
    highest_targets = detector_target_count(online_detectors)
    monitoring = detector_is_monitoring(online_detectors)
    channel_monitoring = detector_channel_is_monitoring(online_detectors)
    channel_count = detector_channel_count(online_detectors)

    add_check(
        checks,
        "clients_endpoint",
        isinstance(heartbeats, list),
        f"{len(heartbeats)} heartbeat(s)",
    )
    if args.expect_detector:
        add_check(
            checks,
            "detector_online",
            bool(online_detectors),
            f"{len(online_detectors)} online detector client(s)",
        )
    if args.expect_alert_client:
        add_check(
            checks,
            "alert_client_online",
            bool(online_alert_clients),
            f"{len(online_alert_clients)} online alert client(s)",
        )
    if args.expect_alert_mode:
        add_check(
            checks,
            "alert_client_mode",
            alert_client_has_mode(online_alert_clients, args.expect_alert_mode),
            (
                f"modes={alert_modes or []} "
                f"expected={args.expect_alert_mode}"
            ),
        )
    if args.expect_alert_popup:
        add_check(
            checks,
            "alert_client_popup",
            alert_popup_enabled,
            f"popup={alert_popup_enabled}",
        )
    if args.expect_alert_details:
        add_check(
            checks,
            "alert_client_details",
            alert_details_enabled,
            f"details={alert_details_enabled}",
        )
    if args.expect_alert_healthy:
        add_check(
            checks,
            "alert_client_healthy",
            alert_healthy,
            f"healthy={alert_healthy}",
        )
    if args.expect_channel_client:
        add_check(
            checks,
            "channel_client_online",
            bool(online_channel_clients),
            f"{len(online_channel_clients)} online channel client(s)",
        )
    if args.expect_monitoring:
        add_check(checks, "detector_monitoring", monitoring, str(monitoring))
    if args.expect_channel_monitoring:
        add_check(
            checks,
            "detector_channel_monitoring",
            channel_monitoring,
            f"{channel_count} monitored channel(s)",
        )
    if args.min_targets > 0:
        add_check(
            checks,
            "detector_targets",
            highest_targets >= args.min_targets,
            f"highest target_count={highest_targets} expected>={args.min_targets}",
        )

    alerts = api.list_alerts(limit=max(0, int(args.limit_alerts)))
    endpoints.append(
        endpoint_record(
            f"{base_url(args.server)}/api/v1/alerts?"
            f"{urlencode({'limit': str(max(0, int(args.limit_alerts)))})}",
            isinstance(alerts, list),
            "recent alerts",
            count=len(alerts),
        )
    )
    add_check(checks, "alerts_endpoint", isinstance(alerts, list), f"{len(alerts)} alert(s)")
    alert_detail = None
    if args.check_alert_detail:
        detail_id = alert_detail_id(alerts)
        if detail_id:
            detail_url = f"{base_url(args.server)}/api/v1/alerts/{detail_id}"
            try:
                alert_detail = api.alert_detail(detail_id)
            except IntelApiError as exc:
                endpoints.append(endpoint_record(detail_url, False, str(exc)))
                add_check(checks, "alert_detail", False, str(exc))
            else:
                endpoints.append(
                    endpoint_record(
                        detail_url,
                        isinstance(alert_detail, dict),
                        f"alert_id={detail_id}",
                    )
                )
                add_check(
                    checks,
                    "alert_detail",
                    isinstance(alert_detail, dict),
                    f"alert_id={detail_id}",
                )
        else:
            add_check(checks, "alert_detail", True, "skipped:no recent alerts")
    active_payload = api.get_active_intel(limit=50)
    active_rows = active_payload.get("active_intel", [])
    if not isinstance(active_rows, list):
        active_rows = []
    endpoints.append(
        endpoint_record(
            f"{base_url(args.server)}/api/v1/active-intel?{urlencode({'limit': '50'})}",
            isinstance(active_payload, dict),
            "active intel rows",
            count=len(active_rows),
        )
    )
    active_ocr = active_source_rows(active_rows, "eve-sentry-detector")
    active_channel = active_source_rows(active_rows, "intel_channel")
    active_ocr_targets = active_ocr_target_count(active_ocr)
    if args.require_active_intel:
        add_check(
            checks,
            "active_intel_present",
            bool(active_rows),
            f"{len(active_rows)} active row(s)",
        )
    else:
        add_check(
            checks,
            "active_intel_endpoint",
            isinstance(active_payload, dict),
            f"{len(active_rows)} active row(s)",
        )
    if args.min_active_ocr_targets > 0:
        add_check(
            checks,
            "active_ocr_targets",
            active_ocr_targets >= args.min_active_ocr_targets,
            (
                f"{active_ocr_targets} active OCR target(s) "
                f"expected>={args.min_active_ocr_targets}"
            ),
        )
    esi = None
    if args.check_esi:
        esi = api.esi_status()
        endpoints.append(
            endpoint_record(
                f"{base_url(args.server)}/api/v1/esi/status",
                isinstance(esi, dict),
                "esi status",
            )
        )
        add_check(
            checks,
            "esi_status",
            isinstance(esi, dict),
            f"enabled={bool(esi.get('enabled'))} authenticated={bool(esi.get('authenticated'))}",
        )
    map_snapshot = None
    if args.check_map:
        map_snapshot = api.map_snapshot()
        systems = map_snapshot.get("systems", [])
        links = map_snapshot.get("links", [])
        endpoints.append(
            endpoint_record(
                f"{base_url(args.server)}/api/v1/map",
                isinstance(systems, list) and isinstance(links, list),
                "map snapshot",
                count=len(systems) if isinstance(systems, list) else 0,
            )
        )
        add_check(
            checks,
            "map_snapshot",
            isinstance(systems, list) and isinstance(links, list),
            f"{len(systems) if isinstance(systems, list) else 0} systems / "
            f"{len(links) if isinstance(links, list) else 0} links",
        )

    ok = all(item["ok"] for item in checks)
    payload = {
        "ok": ok,
        "server": base_url(args.server),
        "read_only": True,
        "schema_version": "integration_status.v2",
        "evidence": {
            "generated_at": utc_now_iso(),
            "checked_urls": checked_urls(args),
            "endpoints": endpoints,
            "expected_conditions": expected_conditions(args),
            "write_endpoints_called": [],
            "notes": "GET-only status check; does not create intel, heartbeat, or acknowledgements.",
        },
        "detectors": [compact_heartbeat(item) for item in detectors],
        "alert_clients": [compact_heartbeat(item) for item in alert_clients],
        "channel_clients": [compact_heartbeat(item) for item in channel_clients],
        "detector_channel": detector_channel_states(detectors),
        "active_ocr": active_ocr,
        "active_channel": active_channel,
        "recent_alerts": alerts,
        "recent_alert_detail": alert_detail,
        "checks": checks,
        "summary": {
            "client_count": len(heartbeats),
            "detector_count": len(detectors),
            "online_detector_count": len(online_detectors),
            "alert_client_count": len(alert_clients),
            "online_alert_client_count": len(online_alert_clients),
            "alert_client_modes": alert_modes,
            "alert_client_popup": alert_popup_enabled,
            "alert_client_details": alert_details_enabled,
            "alert_client_healthy": alert_healthy,
            "channel_client_count": len(channel_clients),
            "online_channel_client_count": len(online_channel_clients),
            "detector_monitoring": monitoring,
            "detector_target_count": highest_targets,
            "detector_channel_monitoring": channel_monitoring,
            "detector_channel_count": channel_count,
            "recent_alert_count": len(alerts),
            "active_intel_count": len(active_rows),
            "active_ocr_count": len(active_ocr),
            "active_ocr_target_count": active_ocr_targets,
            "active_channel_count": len(active_channel),
        },
        "health": {
            "schema_version": health.get("schema_version", ""),
            "generated_at": health.get("generated_at", ""),
            "storage": health.get("storage", {}),
            "events": health.get("events", {}),
        },
        "esi": esi,
        "map": {
            "system_count": len(map_snapshot.get("systems", []))
            if isinstance(map_snapshot, dict) and isinstance(map_snapshot.get("systems"), list)
            else None,
            "link_count": len(map_snapshot.get("links", []))
            if isinstance(map_snapshot, dict) and isinstance(map_snapshot.get("links"), list)
            else None,
        }
        if map_snapshot is not None
        else None,
    }
    return redact_sensitive(payload)


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Server: {payload['server']}",
        f"Read only: {payload['read_only']}",
        f"Overall: {'OK' if payload['ok'] else 'FAILED'}",
        "Checks:",
    ]
    for check in payload["checks"]:
        mark = "OK" if check["ok"] else "FAIL"
        lines.append(f"  [{mark}] {check['name']}: {check['detail']}")
    summary = payload["summary"]
    query = urlencode({"limit": "20"})
    lines.extend(
        [
            "Summary:",
            f"  detector clients: {summary['online_detector_count']}/{summary['detector_count']} online",
            f"  alert clients: {summary['online_alert_client_count']}/{summary['alert_client_count']} online",
            f"  alert client modes: {summary['alert_client_modes']}",
            f"  alert client popup/details: {summary['alert_client_popup']}/{summary['alert_client_details']}",
            f"  alert client healthy: {summary['alert_client_healthy']}",
            f"  detector monitoring: {summary['detector_monitoring']}",
            f"  detector targets: {summary['detector_target_count']}",
            f"  recent alerts read: {summary['recent_alert_count']}",
            f"  active intel rows: {summary['active_intel_count']}",
            f"  active OCR rows: {summary['active_ocr_count']}",
            f"  active OCR targets: {summary['active_ocr_target_count']}",
            f"  active channel rows: {summary['active_channel_count']}",
            f"Alerts URL: {payload['server']}/api/v1/alerts?{query}",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = build_status(args)
    except (IntelApiError, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "server": base_url(args.server),
            "read_only": True,
            "schema_version": "integration_status.v2",
            "evidence": {
                "generated_at": utc_now_iso(),
                "checked_urls": checked_urls(args),
                "expected_conditions": expected_conditions(args),
                "write_endpoints_called": [],
                "notes": "GET-only status check failed before all endpoints could be read.",
            },
            "error": str(exc),
            "checks": [{"name": "connect", "ok": False, "detail": str(exc)}],
        }
    if args.output is not None:
        write_output(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(render_text(payload) if "summary" in payload else payload["error"])
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
