"""Run a local channel-intel smoke test without touching the EVE client."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.channel_client import process_once
from app.channels.log_watcher import ChatLogWatcher
from app.intel.scoring import ScoringEngine
from app.intel_client import IntelApiClient, IntelApiError
from app.server.http_server import IntelHTTPServer
from app.server.intel_store import IntelStore


DEFAULT_SAMPLE_DIR = REPO_ROOT / "samples" / "Chatlogs"


def run_smoke(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Run channel ingestion against an in-process local intel server."""
    log_dir = Path(args.log_dir)
    with tempfile.TemporaryDirectory(prefix="eve-sentry-channel-smoke-") as tmp:
        tmp_path = Path(tmp)
        store = IntelStore(
            tmp_path / "intel_reports.json",
            systems={},
            links=[],
            scorer=ScoringEngine(cooldown_seconds=0),
        )
        server = IntelHTTPServer(store, port=0)
        server.start()
        try:
            api = IntelApiClient(server.url, timeout=args.timeout)
            watcher = ChatLogWatcher(
                log_dir=log_dir,
                channels=args.channel,
                state_path=tmp_path / "channel_offsets.json",
            )
            posted = process_once(watcher, api)
            observations = api.list_observations(limit=args.limit)
            alerts = api.list_alerts(limit=args.limit)
        except IntelApiError as exc:
            return 1, {
                "ok": False,
                "error": str(exc),
                "log_dir": str(log_dir),
                "server": server.url,
            }
        finally:
            server.stop()

    ok = posted > 0 and len(alerts) > 0
    return (
        0 if ok else 1,
        {
            "ok": ok,
            "log_dir": str(log_dir),
            "server": server.url,
            "posted": posted,
            "observation_count": len(observations),
            "alert_count": len(alerts),
            "observations": observations,
            "alerts": alerts,
        },
    )


def print_result(result: dict[str, Any], json_output: bool = False) -> None:
    """Print smoke-test results for humans or automation."""
    if json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return

    status = "OK" if result.get("ok") else "FAILED"
    print(f"Channel smoke {status}")
    print(f"Log dir: {result.get('log_dir')}")
    print(f"Posted observations: {result.get('posted', 0)}")
    print(f"Generated alerts: {result.get('alert_count', 0)}")
    if result.get("error"):
        print(f"Error: {result['error']}")
    for alert in result.get("alerts", [])[:5]:
        system = alert.get("system_name") or alert.get("system") or "Unknown"
        level = str(alert.get("level") or "low").upper()
        score = alert.get("score")
        names = ", ".join(str(name) for name in alert.get("names", []))
        print(f"- {level} {system}: {names} (score {score})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", default=str(DEFAULT_SAMPLE_DIR))
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="channel name filter; can be specified multiple times",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--json",
        action="store_true",
        help="print smoke result as one JSON object",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exit_code, result = run_smoke(args)
    print_result(result, json_output=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
