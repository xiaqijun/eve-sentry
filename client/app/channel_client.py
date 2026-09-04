"""Standalone client that uploads EVE intel channel log lines."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

from app.channels.log_watcher import DEFAULT_CHATLOG_DIR, ChatLogWatcher
from app.core.client_identity import persistent_client_id
from app.core.heartbeat import (
    build_channel_heartbeat_details,
    heartbeat_now_iso,
    resolve_runtime_identity,
    summarize_heartbeat_error,
)
from app.intel_client import IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)


def _send_heartbeat(
    api: IntelApiClient,
    client_id: str,
    interval_seconds: float,
    server_parse: bool,
    last_action: str = "",
    last_error: str = "",
    client_version: str = "",
    host: str = "",
    last_success_at: str = "",
) -> None:
    api.post_heartbeat(
        client_id=client_id,
        client_type="channel_client",
        label="Channel Client",
        heartbeat_interval_seconds=interval_seconds,
        details=build_channel_heartbeat_details(
            server_parse=server_parse,
            last_action=last_action,
            last_error=last_error,
            client_version=client_version,
            host=host,
            last_success_at=last_success_at,
        ),
    )


def run_channel_client(args: argparse.Namespace) -> int:
    """Run the chatlog watcher loop."""
    selected_channels = list(args.channel or [])
    channels_selected = bool(selected_channels or args.all_channels)
    watcher = ChatLogWatcher(
        log_dir=args.log_dir,
        channels=selected_channels,
        state_path=args.state,
        start_at_end_for_new_files=args.ignore_existing,
    )
    api = (
        None
        if args.dry_run
        else IntelApiClient(
            args.server,
            timeout=args.timeout,
            api_key=args.api_key,
        )
    )
    status_stream = sys.stderr if args.json else sys.stdout

    if channels_selected and args.ignore_existing:
        watcher.seed_to_end()

    print(f"Channel client watching {watcher.log_dir}", file=status_stream)
    if not channels_selected:
        print(
            "No channel selected; chatlog lines will not be scanned or posted",
            file=status_stream,
        )
    if args.dry_run:
        print(
            "Dry-run mode: raw channel lines will not be posted",
            file=status_stream,
        )
    else:
        print(
            f"Posting raw channel lines to {args.server} for server-side parsing",
            file=status_stream,
        )
    heartbeat_client_id = persistent_client_id("channel")
    runtime_identity = resolve_runtime_identity()
    heartbeat_interval = max(5.0, float(args.interval))
    last_heartbeat_at = 0.0
    heartbeat_action = "starting"
    heartbeat_error = ""
    heartbeat_last_success_at = ""
    try:
        while True:
            now = time.monotonic()
            if api is not None and not args.once and now >= last_heartbeat_at:
                try:
                    _send_heartbeat(
                        api,
                        heartbeat_client_id,
                        heartbeat_interval,
                        True,
                        last_action=heartbeat_action,
                        last_error=heartbeat_error,
                        client_version=runtime_identity["client_version"],
                        host=runtime_identity["host"],
                        last_success_at=heartbeat_last_success_at,
                    )
                except IntelApiError as exc:
                    logger.warning("Heartbeat update failed: %s", exc)
                last_heartbeat_at = now + heartbeat_interval
            diagnostics = {
                "last_action": "",
                "last_error": heartbeat_error,
                "last_success_at": heartbeat_last_success_at,
            }
            if channels_selected:
                processed = process_once(
                    watcher,
                    api,
                    dry_run=args.dry_run,
                    json_lines=args.json,
                    diagnostics=diagnostics,
                )
            else:
                processed = 0
                diagnostics["last_action"] = "channel_unselected"
                diagnostics["last_error"] = ""
            heartbeat_action = str(diagnostics.get("last_action") or heartbeat_action)
            heartbeat_error = str(diagnostics.get("last_error") or "")
            heartbeat_last_success_at = str(
                diagnostics.get("last_success_at") or heartbeat_last_success_at
            )
            if args.once:
                if api is not None:
                    try:
                        _send_heartbeat(
                            api,
                            heartbeat_client_id,
                            heartbeat_interval,
                            True,
                            last_action=heartbeat_action,
                            last_error=heartbeat_error,
                            client_version=runtime_identity["client_version"],
                            host=runtime_identity["host"],
                            last_success_at=heartbeat_last_success_at,
                        )
                    except IntelApiError as exc:
                        logger.warning("Heartbeat update failed: %s", exc)
                action = "Read" if args.dry_run else "Posted"
                print(f"{action} {processed} channel lines", file=status_stream)
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def process_once(
    watcher: ChatLogWatcher,
    api: IntelApiClient | None,
    dry_run: bool = False,
    json_lines: bool = False,
    stream: Any | None = None,
    diagnostics: dict[str, str] | None = None,
) -> int:
    """Read available lines once and post raw channel records."""
    if api is None and not dry_run:
        raise ValueError("api is required unless dry_run is enabled")

    stream = stream or sys.stdout
    processed = 0
    mode = "server_parse"
    for line in watcher.poll_lines():
        if not dry_run:
            try:
                assert api is not None
                result = api.post_channel_line(
                    line.text,
                    channel=line.channel,
                    defer_enrichment=True,
                )
            except IntelApiError as exc:
                logger.warning("Failed to post channel line: %s", exc)
                if diagnostics is not None:
                    diagnostics["last_action"] = "server_parse_error"
                    diagnostics["last_error"] = summarize_heartbeat_error(str(exc))
                break
            if not result.get("ignored"):
                processed += 1
            watcher.commit_line(line)
            if diagnostics is not None:
                diagnostics["last_action"] = (
                    f"server_parse:{processed}" if processed else "server_parse_idle"
                )
                diagnostics["last_error"] = ""
                diagnostics["last_success_at"] = heartbeat_now_iso()
            continue

        emit_channel_line(line, json_lines=json_lines, stream=stream)
        processed += 1
        watcher.commit_line(line)
        if diagnostics is not None:
            diagnostics["last_action"] = f"dry_run_raw:{processed}"
            diagnostics["last_error"] = ""
            diagnostics["last_success_at"] = heartbeat_now_iso()
        continue

    if diagnostics is not None and not diagnostics.get("last_action"):
        diagnostics["last_action"] = f"{mode}_idle"
        diagnostics["last_error"] = ""
    return processed


def emit_channel_line(
    line: Any,
    json_lines: bool = False,
    stream: Any | None = None,
) -> None:
    """Print a raw channel line for dry-run verification."""
    stream = stream or sys.stdout
    payload = {
        "channel": getattr(line, "channel", ""),
        "text": getattr(line, "text", ""),
    }
    if json_lines:
        print(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            file=stream,
            flush=True,
        )
        return
    channel = str(payload["channel"] or "").strip()
    prefix = f"[{channel}] " if channel else ""
    print(f"[RAW] {prefix}{payload['text']}", file=stream, flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("EVE_SENTRY_API_KEY", ""),
        help="optional device key; prefer EVE_SENTRY_API_KEY to avoid command-line exposure",
    )
    parser.add_argument("--log-dir", default=str(DEFAULT_CHATLOG_DIR))
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="exact channel name; use * or ? for explicit wildcards; can be specified multiple times",
    )
    parser.add_argument(
        "--all-channels",
        action="store_true",
        help="explicitly monitor every chatlog channel; by default no --channel means no upload",
    )
    parser.add_argument("--state", default="channel_offsets.json")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print raw channel lines without posting to the server",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print dry-run channel lines as JSON Lines",
    )
    parser.add_argument(
        "--include-existing",
        action="store_false",
        dest="ignore_existing",
        help="post existing chatlog lines when the client starts",
    )
    args = parser.parse_args(argv)
    if not args.dry_run and not str(args.server or "").strip():
        parser.error("--server is required unless --dry-run is used")
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return run_channel_client(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
