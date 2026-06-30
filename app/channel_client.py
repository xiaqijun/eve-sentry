"""Standalone client that parses EVE intel channel logs and posts observations."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

from app.channels.log_watcher import DEFAULT_CHATLOG_DIR, ChatLogWatcher
from app.channels.parser import parse_chat_line
from app.intel_client import IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)


def run_channel_client(args: argparse.Namespace) -> int:
    """Run the chatlog watcher loop."""
    watcher = ChatLogWatcher(
        log_dir=args.log_dir,
        channels=args.channel,
        state_path=args.state,
    )
    api = None if args.dry_run else IntelApiClient(args.server, timeout=args.timeout)
    status_stream = sys.stderr if args.json else sys.stdout

    if args.ignore_existing:
        watcher.seed_to_end()

    print(f"Channel client watching {watcher.log_dir}", file=status_stream)
    if args.dry_run:
        print(
            "Dry-run mode: parsed observations will not be posted",
            file=status_stream,
        )
    else:
        print(f"Posting observations to {args.server}", file=status_stream)
    try:
        while True:
            processed = process_once(
                watcher,
                api,
                dry_run=args.dry_run,
                json_lines=args.json,
            )
            if args.once:
                action = "Parsed" if args.dry_run else "Posted"
                print(f"{action} {processed} observations", file=status_stream)
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
) -> int:
    """Read available lines once and post parsed observations."""
    if api is None and not dry_run:
        raise ValueError("api is required unless dry_run is enabled")

    stream = stream or sys.stdout
    processed = 0
    for line in watcher.poll_lines():
        parsed = parse_chat_line(line.text, channel=line.channel)
        if parsed is None:
            continue
        payload = parsed.to_observation_payload()
        if dry_run:
            emit_observation(payload, json_lines=json_lines, stream=stream)
            processed += 1
            continue

        try:
            assert api is not None
            api.post_observation(**payload_to_client_args(payload))
        except IntelApiError as exc:
            logger.warning("Failed to post channel observation: %s", exc)
            continue
        processed += 1
    return processed


def emit_observation(
    payload: dict[str, Any],
    json_lines: bool = False,
    stream: Any | None = None,
) -> None:
    """Print a parsed observation for dry-run verification."""
    stream = stream or sys.stdout
    if json_lines:
        print(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            file=stream,
            flush=True,
        )
        return
    print(f"[OBS] {format_observation(payload)}", file=stream, flush=True)


def format_observation(payload: dict[str, Any]) -> str:
    """Return a compact one-line dry-run observation summary."""
    system = str(payload.get("system_name") or "Unknown")
    raw_text = str(payload.get("raw_text") or "").strip()
    raw_metadata = payload.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    details: list[str] = []
    hostile_count = metadata.get("hostile_count")
    if hostile_count not in {None, ""}:
        details.append(f"{hostile_count} hostile")
        if str(hostile_count) != "1":
            details[-1] += "s"
    jump_count = metadata.get("jump_count")
    if jump_count not in {None, ""}:
        suffix = "jump" if str(jump_count) == "1" else "jumps"
        details.append(f"{jump_count} {suffix}")
    direction = str(metadata.get("direction") or "").strip()
    if direction:
        details.append(f"toward {direction}")

    suffix = f" ({'; '.join(details)})" if details else ""
    if raw_text:
        return f"{system}: {raw_text}{suffix}"
    return f"{system}{suffix}"


def payload_to_client_args(payload: dict) -> dict:
    """Translate parser payload keys into IntelApiClient.post_observation args."""
    return {
        "system_name": payload["system_name"],
        "names": payload.get("names", []),
        "source": payload.get("source", "intel_channel"),
        "source_instance": payload.get("source_instance", ""),
        "confidence": payload.get("confidence"),
        "raw_text": payload.get("raw_text", ""),
        "metadata": payload.get("metadata"),
        "seen_at": payload.get("seen_at"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--log-dir", default=str(DEFAULT_CHATLOG_DIR))
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="channel name filter; can be specified multiple times",
    )
    parser.add_argument("--state", default="channel_offsets.json")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and print observations without posting to the server",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print dry-run observations as JSON Lines",
    )
    parser.add_argument(
        "--include-existing",
        action="store_false",
        dest="ignore_existing",
        help="post existing chatlog lines when the client starts",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return run_channel_client(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
