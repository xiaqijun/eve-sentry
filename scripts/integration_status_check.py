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
        "--expect-monitoring",
        action="store_true",
        help="fail unless a detector client reports running/monitoring state",
    )
    parser.add_argument(
        "--min-targets",
        type=int,
        default=0,
        help="fail unless detector heartbeat details include at least this many targets",
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
            sample = response.read(256).decode("utf-8", errors="replace")
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
    if item.get("online") is True:
        return True
    status = str(item.get("status") or "").strip().lower()
    stale = bool(item.get("stale"))
    return bool(status) and status != "offline" and not stale


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


def compact_heartbeat(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details", {})
    if not isinstance(details, dict):
        details = {}
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
        "detector_monitoring": bool(args.expect_monitoring),
        "min_targets": max(0, int(args.min_targets)),
        "event_health": bool(args.require_event_health),
        "active_intel": bool(args.require_active_intel),
        "esi_status": bool(args.check_esi),
        "map_snapshot": bool(args.check_map),
        "events_stream": bool(args.check_events_stream),
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
    highest_targets = detector_target_count(detectors)
    monitoring = detector_is_monitoring(detectors)

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
    if args.expect_monitoring:
        add_check(checks, "detector_monitoring", monitoring, str(monitoring))
    if args.min_targets > 0:
        add_check(
            checks,
            "detector_targets",
            highest_targets >= args.min_targets,
            f"highest target_count={highest_targets}",
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
    return {
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
        "checks": checks,
        "summary": {
            "client_count": len(heartbeats),
            "detector_count": len(detectors),
            "online_detector_count": len(online_detectors),
            "alert_client_count": len(alert_clients),
            "online_alert_client_count": len(online_alert_clients),
            "channel_client_count": len(channel_clients),
            "online_channel_client_count": len(online_channel_clients),
            "detector_monitoring": monitoring,
            "detector_target_count": highest_targets,
            "recent_alert_count": len(alerts),
            "active_intel_count": len(active_rows),
            "active_ocr_count": len(active_ocr),
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
            f"  detector monitoring: {summary['detector_monitoring']}",
            f"  detector targets: {summary['detector_target_count']}",
            f"  recent alerts read: {summary['recent_alert_count']}",
            f"  active intel rows: {summary['active_intel_count']}",
            f"  active OCR rows: {summary['active_ocr_count']}",
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
