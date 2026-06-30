"""Standalone alert client that subscribes to the intel server."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from app.intel_client import AlertPoller, IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)


def format_report(report: dict[str, Any]) -> str:
    """Return a compact one-line summary for a report-like payload."""
    system = str(report.get("system_name") or report.get("system") or "Unknown")
    names = report.get("names") or []
    if not isinstance(names, list):
        names = []
    joined_names = ", ".join(str(name) for name in names) or "Unknown target"
    seen_at = str(report.get("seen_at") or report.get("created_at") or "")
    return f"{seen_at} {system}: {joined_names}".strip()


def format_alert(alert: dict[str, Any]) -> str:
    """Return a compact one-line summary for a threat event."""
    base = format_report(alert)
    level = str(alert.get("level") or "low").upper()
    score = alert.get("score")
    if score is None:
        text = f"{level} {base}".strip()
    else:
        text = f"{level} {base} (score {score})".strip()

    evidence = _format_evidence_summary(alert)
    if evidence:
        return f"{text} - {evidence}"
    return text


def _format_evidence_summary(alert: dict[str, Any]) -> str:
    evidence = alert.get("evidence")
    if not isinstance(evidence, list):
        return ""

    summaries: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or item.get("type") or "").strip()
        if summary:
            summaries.append(summary)
        if len(summaries) >= 2:
            break
    return "; ".join(summaries)


def build_popup_names(reports: list[dict[str, Any]]) -> list[str]:
    """Build popup list entries from reports or threat events."""
    entries: list[str] = []
    for report in reports:
        system = str(report.get("system_name") or report.get("system") or "Unknown")
        names = report.get("names") or []
        if not isinstance(names, list):
            continue
        for name in names:
            text = str(name).strip()
            if text:
                entries.append(f"{system} - {text}")
    return entries


def show_popup(entries: list[str]) -> None:
    """Show the existing alert dialog for new intel entries."""
    if not entries:
        return
    from PyQt6.QtWidgets import QApplication

    from app.ui.alert_dialog import AlertDialog

    app = QApplication.instance() or QApplication([])
    dialog = AlertDialog(entries)
    dialog.exec()
    app.processEvents()


def run_alert_client(args: argparse.Namespace) -> int:
    """Run the polling alert loop."""
    api = IntelApiClient(args.server, timeout=args.timeout)
    poller = AlertPoller(api, limit=args.limit)

    if args.ignore_existing:
        try:
            poller.seed_existing()
        except IntelApiError as exc:
            logger.warning("Initial alert sync failed: %s", exc)

    print(f"Alert client listening on {args.server}")
    print("Press Ctrl+C to stop.")
    use_events = not args.poll
    try:
        while True:
            try:
                if use_events:
                    alerts = poller.stream_new(timeout=args.interval)
                else:
                    alerts = poller.poll_new()
            except IntelApiError as exc:
                if use_events:
                    logger.warning("Event stream failed, falling back to polling: %s", exc)
                    use_events = False
                    continue
                logger.warning("Polling failed: %s", exc)
                time.sleep(args.interval)
                continue

            if alerts:
                for alert in alerts:
                    print(f"[ALERT] {format_alert(alert)}", flush=True)
                if args.popup:
                    show_popup(build_popup_names(alerts))

            if not use_events:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--include-existing",
        action="store_false",
        dest="ignore_existing",
        help="alert for events that already exist when the client starts",
    )
    parser.add_argument(
        "--popup",
        action="store_true",
        help="show a local popup and play the alert sound for new events",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="use /api/alerts polling instead of the event stream",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return run_alert_client(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
