"""Import legacy JSON intel reports into the SQLite store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.server.intel_store import IntelStore
from app.server.sqlite_store import SQLiteIntelStore


def run_import(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Run the JSON-to-SQLite import and return a CLI-friendly result."""
    source_path = Path(args.source)
    db_path = Path(args.db)
    if not source_path.exists():
        return 1, {
            "ok": False,
            "error": f"source JSON not found: {source_path}",
            "source": str(source_path),
            "db": str(db_path),
        }

    legacy_store = IntelStore(source_path, systems={}, links=[])
    reports = legacy_store._reports_snapshot()
    existing_count = _existing_report_count(db_path)

    if existing_count and not args.replace:
        return 1, {
            "ok": False,
            "error": "target database already contains reports; use --replace",
            "source": str(source_path),
            "db": str(db_path),
            "source_count": len(reports),
            "existing_count": existing_count,
        }

    result = {
        "ok": True,
        "source": str(source_path),
        "db": str(db_path),
        "source_count": len(reports),
        "existing_count": existing_count,
        "imported_count": 0 if args.dry_run else len(reports),
        "dry_run": bool(args.dry_run),
        "replaced": bool(existing_count and args.replace),
    }
    if args.dry_run:
        return 0, result

    target_store = SQLiteIntelStore(db_path, systems={}, links=[])
    target_store._replace_reports(reports)
    target_store._set_meta("legacy_json_imported", "1")
    reloaded_store = SQLiteIntelStore(db_path, systems={}, links=[])
    result["final_count"] = len(reloaded_store.list_reports())
    return 0, result


def _existing_report_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    return len(SQLiteIntelStore(db_path, systems={}, links=[]).list_reports())


def print_result(result: dict[str, Any], json_output: bool = False) -> None:
    """Print import results for humans or automation."""
    if json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return

    if not result.get("ok"):
        print(f"Import failed: {result.get('error')}", file=sys.stderr)
        return
    mode = "would import" if result.get("dry_run") else "imported"
    print(
        f"SQLite import {mode} {result.get('source_count', 0)} reports "
        f"from {result.get('source')} to {result.get('db')}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="intel_reports.json")
    parser.add_argument("--db", default="intel.sqlite3")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace existing SQLite reports instead of refusing the import",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and report counts without writing the SQLite database",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the result as one JSON object",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exit_code, result = run_import(args)
    print_result(result, json_output=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
