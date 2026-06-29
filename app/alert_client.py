"""Standalone alert client that subscribes to the intel server."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from app.intel_client import IntelApiClient, IntelApiError, ReportPoller

logger = logging.getLogger(__name__)


def format_report(report: dict[str, Any]) -> str:
    """Return a compact one-line summary for a report."""
    system = str(report.get("system") or "未知星系")
    names = report.get("names") or []
    if not isinstance(names, list):
        names = []
    joined_names = ", ".join(str(name) for name in names) or "未知目标"
    seen_at = str(report.get("seen_at") or "")
    return f"{seen_at} {system}: {joined_names}".strip()


def build_popup_names(reports: list[dict[str, Any]]) -> list[str]:
    """Build popup list entries from one or more reports."""
    entries: list[str] = []
    for report in reports:
        system = str(report.get("system") or "未知星系")
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
    poller = ReportPoller(api, limit=args.limit)

    if args.ignore_existing:
        try:
            poller.seed_existing()
        except IntelApiError as exc:
            logger.warning("Initial report sync failed: %s", exc)

    print(f"Alert client listening on {args.server}")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            try:
                reports = poller.poll_new()
            except IntelApiError as exc:
                logger.warning("Polling failed: %s", exc)
                time.sleep(args.interval)
                continue

            if reports:
                for report in reports:
                    print(f"[ALERT] {format_report(report)}", flush=True)
                if args.popup:
                    show_popup(build_popup_names(reports))

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
        help="alert for reports that already exist when the client starts",
    )
    parser.add_argument(
        "--popup",
        action="store_true",
        help="show a local popup and play the alert sound for new reports",
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
