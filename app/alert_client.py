"""Standalone alert client that subscribes to the intel server."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from app.intel_client import AlertPoller, IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)


class AlertClientState:
    """Persist recently emitted alert ids so restarts can resume safely."""

    def __init__(
        self,
        path: str | Path = "alert_client_state.json",
        max_seen_ids: int = 1000,
    ) -> None:
        self.path = Path(path)
        self.max_seen_ids = max(1, int(max_seen_ids))
        self.loaded = False
        self._seen_ids: list[str] = []

    def load_seen_ids(self) -> list[str]:
        """Load the remembered alert ids from disk."""
        self.loaded = self.path.exists()
        if not self.loaded:
            self._seen_ids = []
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read alert client state from %s", self.path)
            self._seen_ids = []
            return []
        if not isinstance(data, dict):
            self._seen_ids = []
            return []
        self._seen_ids = self._clean_ids(data.get("seen_alert_ids"))
        return list(self._seen_ids)

    def save_seen_ids(self, seen_ids: list[str]) -> None:
        """Persist a normalized set of recently seen alert ids."""
        self._seen_ids = self._clean_ids(seen_ids)
        payload = {"version": 1, "seen_alert_ids": self._seen_ids}
        try:
            if self.path.parent and not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.loaded = True
        except OSError:
            logger.warning("Failed to write alert client state to %s", self.path)

    def record_alerts(self, alerts: list[dict[str, Any]]) -> None:
        """Append emitted alert ids to the state file."""
        ids = list(self._seen_ids)
        for alert in alerts:
            alert_id = str(alert.get("id") or "").strip()
            if not alert_id:
                continue
            if alert_id in ids:
                ids.remove(alert_id)
            ids.append(alert_id)
        self.save_seen_ids(ids)

    def _clean_ids(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        ids = []
        seen = set()
        for item in value[-self.max_seen_ids:]:
            alert_id = str(item or "").strip()
            if not alert_id or alert_id in seen:
                continue
            seen.add(alert_id)
            ids.append(alert_id)
        return ids


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
    detail_context = _format_detail_context_summary(alert)
    suffixes = [item for item in (evidence, detail_context) if item]
    if suffixes:
        return f"{text} - {' | '.join(suffixes)}"
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


def _format_detail_context_summary(alert: dict[str, Any]) -> str:
    detail = alert.get("detail")
    if not isinstance(detail, dict):
        return ""
    server_summary = _format_explanation_context(detail.get("explanation"))
    if server_summary:
        return server_summary

    context = detail.get("context")
    if not isinstance(context, dict):
        return ""

    parts: list[str] = []
    parts.extend(_format_channel_context(context.get("channel_mentions")))
    parts.extend(_format_profile_context(context.get("character_profiles")))
    parts.extend(_format_kill_context(context.get("kill_activities")))
    parts.extend(_format_group_context(context.get("group_activities")))
    if not parts:
        return ""
    return f"Context: {'; '.join(parts[:4])}"


def _format_explanation_context(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    context = value.get("context")
    if not isinstance(context, list):
        return ""
    summaries = [str(item).strip() for item in context if str(item).strip()]
    if not summaries:
        return ""
    return f"Context: {'; '.join(summaries[:4])}"


def _format_channel_context(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        observation = item.get("observation")
        if not isinstance(observation, dict):
            continue
        relation = _relation_label(str(item.get("relation") or ""))
        system = str(
            observation.get("system_name") or observation.get("system") or "Unknown"
        )
        parts.append(f"channel {relation} {system}")
    return parts


def _format_profile_context(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or item.get("character_id") or "").strip()
        if not label:
            continue
        affiliations = []
        corporation_id = item.get("corporation_id")
        alliance_id = item.get("alliance_id")
        if corporation_id not in {None, ""}:
            affiliations.append(f"corp {corporation_id}")
        if alliance_id not in {None, ""}:
            affiliations.append(f"alliance {alliance_id}")
        suffix = f" ({', '.join(affiliations)})" if affiliations else ""
        parts.append(f"profile {label}{suffix}")
    return parts


def _format_kill_context(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        character_id = item.get("character_id")
        if character_id in {None, ""}:
            continue
        counts = _activity_counts(item)
        parts.append(f"character {character_id} {counts}")
    return parts


def _format_group_context(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    parts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entity_type = str(item.get("entity_type") or "group")
        entity_id = item.get("entity_id") or item.get(f"{entity_type}_id")
        if entity_id in {None, ""}:
            continue
        counts = _activity_counts(item)
        parts.append(f"{entity_type} {entity_id} {counts}")
    return parts


def _activity_counts(item: dict[str, Any]) -> str:
    parts = []
    if _has_count(item.get("kills")):
        parts.append(_plural_count(item["kills"], "kill"))
    if _has_count(item.get("losses")):
        parts.append(_plural_count(item["losses"], "loss"))
    return ", ".join(parts) or "activity"


def _has_count(value: Any) -> bool:
    if value in {None, ""}:
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return True


def _plural_count(value: Any, label: str) -> str:
    if str(value) == "1":
        return f"{value} {label}"
    plural = "losses" if label == "loss" else f"{label}s"
    return f"{value} {plural}"


def _relation_label(value: str) -> str:
    relation = value.strip().casefold()
    if relation == "same_system":
        return "same-system"
    if relation == "adjacent_system":
        return "adjacent-system"
    return relation.replace("_", "-") or "related"


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


def emit_alerts(
    alerts: list[dict[str, Any]],
    popup: bool = False,
    json_lines: bool = False,
    stream: Any | None = None,
) -> None:
    """Write alerts to a stream and optionally show the popup dialog."""
    stream = stream or sys.stdout
    for alert in alerts:
        if json_lines:
            print(
                json.dumps(alert, ensure_ascii=False, sort_keys=True),
                file=stream,
                flush=True,
            )
        else:
            print(f"[ALERT] {format_alert(alert)}", file=stream, flush=True)
    if popup:
        show_popup(build_popup_names(alerts))


def ack_emitted_alerts(
    api: IntelApiClient,
    alerts: list[dict[str, Any]],
    acknowledged_by: str = "alert-client",
    note: str = "",
) -> int:
    """Acknowledge alerts that were successfully emitted locally."""
    acknowledged = 0
    for alert in alerts:
        alert_id = str(alert.get("id") or "").strip()
        if not alert_id:
            continue
        try:
            api.ack_alert(alert_id, acknowledged_by=acknowledged_by, note=note)
        except IntelApiError as exc:
            logger.warning("Failed to acknowledge alert %s: %s", alert_id, exc)
            continue
        acknowledged += 1
    return acknowledged


def attach_alert_details(
    api: IntelApiClient,
    alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach alert detail payloads without interrupting alert delivery."""
    detailed = []
    for alert in alerts:
        item = dict(alert)
        alert_id = str(item.get("id") or "").strip()
        if not alert_id:
            detailed.append(item)
            continue
        try:
            item["detail"] = api.alert_detail(alert_id)
        except IntelApiError as exc:
            item["detail_error"] = str(exc)
            logger.warning("Failed to fetch alert detail %s: %s", alert_id, exc)
        detailed.append(item)
    return detailed


def run_alert_client(args: argparse.Namespace) -> int:
    """Run the polling alert loop."""
    api = IntelApiClient(args.server, timeout=args.timeout)
    state_store = None
    seen_ids: list[str] = []
    if not args.no_state:
        state_store = AlertClientState(args.state)
        seen_ids = state_store.load_seen_ids()

    poller = AlertPoller(
        api,
        limit=args.limit,
        acknowledged=False if args.unacknowledged_only else None,
        min_score=args.min_score,
        min_level=args.min_level,
        seen_ids=seen_ids,
    )

    if args.ignore_existing and (state_store is None or not state_store.loaded):
        try:
            seeded_alerts = poller.seed_existing()
            if state_store is not None:
                state_store.record_alerts(seeded_alerts)
        except IntelApiError as exc:
            logger.warning("Initial alert sync failed: %s", exc)

    once = getattr(args, "once", False)
    json_lines = getattr(args, "json", False)
    popup = getattr(args, "popup", False)
    ack = getattr(args, "ack", False)
    ack_by = getattr(args, "ack_by", "alert-client")
    ack_note = getattr(args, "ack_note", "")
    details = getattr(args, "details", False)
    status_stream = sys.stderr if json_lines else sys.stdout
    print(f"Alert client listening on {args.server}", file=status_stream)
    if once:
        print("Running one alert check.", file=status_stream)
    else:
        print("Press Ctrl+C to stop.", file=status_stream)
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
                if once:
                    return 1
                time.sleep(args.interval)
                continue

            if alerts:
                if details:
                    alerts = attach_alert_details(api, alerts)
                emit_alerts(
                    alerts,
                    popup=popup,
                    json_lines=json_lines,
                )
                if state_store is not None:
                    state_store.record_alerts(alerts)
                if ack:
                    ack_emitted_alerts(
                        api,
                        alerts,
                        acknowledged_by=ack_by,
                        note=ack_note,
                    )

            if once:
                return 0

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
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one alert check and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print new alerts as JSON Lines",
    )
    parser.add_argument(
        "--ack",
        action="store_true",
        help="acknowledge each alert after it is emitted locally",
    )
    parser.add_argument(
        "--ack-by",
        default="alert-client",
        help="client name recorded when --ack is enabled",
    )
    parser.add_argument(
        "--ack-note",
        default="",
        help="optional acknowledgement note recorded when --ack is enabled",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="fetch alert detail context before printing each alert",
    )
    parser.add_argument(
        "--unacknowledged-only",
        action="store_true",
        help="only consume alerts that have not been acknowledged on the server",
    )
    parser.add_argument(
        "--min-score",
        type=_non_negative_int,
        help="only consume alerts with score greater than or equal to this value",
    )
    parser.add_argument(
        "--min-level",
        choices=["low", "medium", "high", "critical"],
        default="",
        help="only consume alerts at this severity or higher",
    )
    parser.add_argument(
        "--state",
        default="alert_client_state.json",
        help="path to the local alert client resume state file",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="disable the local resume state file",
    )
    return parser.parse_args(argv)


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return number


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return run_alert_client(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
