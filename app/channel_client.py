"""Standalone client that parses EVE intel channel logs and posts observations."""

from __future__ import annotations

import argparse
import logging
import sys
import time

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
    api = IntelApiClient(args.server, timeout=args.timeout)

    if args.ignore_existing:
        watcher.seed_to_end()

    print(f"Channel client watching {watcher.log_dir}")
    print(f"Posting observations to {args.server}")
    try:
        while True:
            posted = process_once(watcher, api)
            if args.once:
                print(f"Posted {posted} observations")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def process_once(watcher: ChatLogWatcher, api: IntelApiClient) -> int:
    """Read available lines once and post parsed observations."""
    posted = 0
    for line in watcher.poll_lines():
        parsed = parse_chat_line(line.text, channel=line.channel)
        if parsed is None:
            continue
        payload = parsed.to_observation_payload()
        try:
            api.post_observation(**payload_to_client_args(payload))
        except IntelApiError as exc:
            logger.warning("Failed to post channel observation: %s", exc)
            continue
        posted += 1
    return posted


def payload_to_client_args(payload: dict) -> dict:
    """Translate parser payload keys into IntelApiClient.post_observation args."""
    return {
        "system_name": payload["system_name"],
        "names": payload.get("names", []),
        "source": payload.get("source", "intel_channel"),
        "source_instance": payload.get("source_instance", ""),
        "confidence": payload.get("confidence"),
        "raw_text": payload.get("raw_text", ""),
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

