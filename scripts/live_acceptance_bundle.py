"""Create a read-only live acceptance evidence bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import integration_status_check


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bundle read-only live EVE Sentry acceptance checks without "
            "creating intel, heartbeats, acknowledgements, or sample data."
        )
    )
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path, default=Path("evidence/live"))
    parser.add_argument("--json", action="store_true", help="emit the manifest JSON")
    parser.add_argument("--expect-detector", action="store_true")
    parser.add_argument("--expect-alert-client", action="store_true")
    parser.add_argument("--expect-alert-mode", choices=["events", "poll"], default="")
    parser.add_argument("--expect-alert-popup", action="store_true")
    parser.add_argument("--expect-alert-details", action="store_true")
    parser.add_argument("--expect-alert-healthy", action="store_true")
    parser.add_argument("--expect-channel-client", action="store_true")
    parser.add_argument("--expect-monitoring", action="store_true")
    parser.add_argument("--expect-channel-monitoring", action="store_true")
    parser.add_argument("--min-targets", type=int, default=0)
    parser.add_argument("--min-active-ocr-targets", type=int, default=0)
    parser.add_argument("--require-event-health", action="store_true")
    parser.add_argument(
        "--require-active-intel",
        action="store_true",
        help="require active intel only when real OCR or channel intel is present",
    )
    parser.add_argument("--check-esi", action="store_true")
    parser.add_argument("--check-map", action="store_true")
    parser.add_argument("--check-events-stream", action="store_true")
    parser.add_argument("--check-alert-detail", action="store_true")
    parser.add_argument("--limit-alerts", type=int, default=5)
    return parser.parse_args(argv)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_slug(value: str) -> str:
    return value.replace(":", "").replace("-", "").split(".")[0].replace("T", "-")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def status_args(args: argparse.Namespace, scenario: str) -> argparse.Namespace:
    argv = [
        "--server",
        args.server,
        "--timeout",
        str(args.timeout),
        "--limit-alerts",
        str(max(0, int(args.limit_alerts))),
    ]
    if args.check_esi:
        argv.append("--check-esi")
    if args.check_map:
        argv.append("--check-map")
    if args.check_events_stream or scenario == "alert-client":
        argv.append("--check-events-stream")
    if args.require_event_health:
        argv.append("--require-event-health")
    if args.require_active_intel:
        argv.append("--require-active-intel")
    if scenario == "detector-channel":
        if args.expect_detector:
            argv.append("--expect-detector")
        if args.expect_monitoring:
            argv.append("--expect-monitoring")
        if args.expect_channel_monitoring:
            argv.append("--expect-channel-monitoring")
        if args.min_targets > 0:
            argv.extend(["--min-targets", str(args.min_targets)])
        if args.min_active_ocr_targets > 0:
            argv.extend(["--min-active-ocr-targets", str(args.min_active_ocr_targets)])
    if scenario == "alert-client" and args.expect_alert_client:
        argv.append("--expect-alert-client")
    if scenario == "alert-client" and args.expect_alert_mode:
        argv.extend(["--expect-alert-mode", args.expect_alert_mode])
    if scenario == "alert-client" and args.expect_alert_popup:
        argv.append("--expect-alert-popup")
    if scenario == "alert-client" and args.expect_alert_details:
        argv.append("--expect-alert-details")
    if scenario == "alert-client" and args.expect_alert_healthy:
        argv.append("--expect-alert-healthy")
    if scenario == "alert-client" and args.check_alert_detail:
        argv.append("--check-alert-detail")
    if scenario == "detector-channel" and args.expect_channel_client:
        argv.append("--expect-channel-client")
    return integration_status_check.parse_args(argv)


def build_bundle(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = utc_now_iso()
    bundle_dir = args.output_dir / timestamp_slug(generated_at)
    scenarios = [
        ("baseline", status_args(args, "baseline")),
        ("detector-channel", status_args(args, "detector-channel")),
        ("alert-client", status_args(args, "alert-client")),
    ]
    files: list[dict[str, Any]] = []
    statuses: dict[str, dict[str, Any]] = {}
    for name, status_arg in scenarios:
        payload = integration_status_check.build_status(status_arg)
        output_path = bundle_dir / f"{name}.json"
        write_json(output_path, payload)
        files.append(
            {
                "name": name,
                "path": str(output_path),
                "ok": bool(payload.get("ok")),
                "schema_version": payload.get("schema_version", ""),
            }
        )
        statuses[name] = payload

    ok = all(item["ok"] for item in files)
    manifest = {
        "ok": ok,
        "schema_version": "live_acceptance_bundle.v1",
        "generated_at": generated_at,
        "server": integration_status_check.base_url(args.server),
        "bundle_dir": str(bundle_dir),
        "read_only": True,
        "files": files,
        "expected": {
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
        },
        "write_endpoints_called": [],
        "notes": (
            "Read-only bundle. Evidence is copied from live server GET endpoints; "
            "this script does not start clients, parse sample chatlogs, create OCR "
            "snapshots, post observations, send heartbeats, or acknowledge alerts."
        ),
        "summary": {
            name: {
                "ok": bool(payload.get("ok")),
                "checks": payload.get("checks", []),
                "summary": payload.get("summary", {}),
            }
            for name, payload in statuses.items()
        },
    }
    write_json(bundle_dir / "manifest.json", manifest)
    return manifest


def render_text(manifest: dict[str, Any]) -> str:
    lines = [
        f"Bundle: {manifest['bundle_dir']}",
        f"Server: {manifest['server']}",
        f"Read only: {manifest['read_only']}",
        f"Overall: {'OK' if manifest['ok'] else 'FAILED'}",
        "Files:",
    ]
    for item in manifest["files"]:
        mark = "OK" if item["ok"] else "FAIL"
        lines.append(f"  [{mark}] {item['name']}: {item['path']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = build_bundle(args)
    except Exception as exc:  # pragma: no cover - surfaced as CLI error.
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    else:
        print(render_text(manifest))
    return 0 if manifest.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
