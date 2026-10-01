"""Run the intel server using environment-driven deployment defaults."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.server import __main__ as server_main


def build_server_argv(env: Mapping[str, str] | None = None) -> list[str]:
    """Translate deployment environment variables into server CLI args."""
    values = env or os.environ
    argv: list[str] = []

    _append_option(argv, "--host", values.get("EVE_SENTRY_SERVER_HOST", ""))
    _append_option(argv, "--port", values.get("EVE_SENTRY_SERVER_PORT", ""))
    _append_option(argv, "--storage", values.get("EVE_SENTRY_SERVER_STORAGE", ""))
    _append_option(argv, "--data", values.get("EVE_SENTRY_SERVER_DATA", ""))
    _append_option(
        argv,
        "--report-retention-days",
        values.get("EVE_SENTRY_SERVER_REPORT_RETENTION_DAYS", ""),
    )
    _append_option(
        argv,
        "--inactive-intel-retention-days",
        values.get("EVE_SENTRY_SERVER_INACTIVE_INTEL_RETENTION_DAYS", ""),
    )
    _append_option(
        argv,
        "--postgres-dsn",
        values.get("EVE_SENTRY_SERVER_POSTGRES_DSN", ""),
    )
    _append_option(
        argv,
        "--hot-report-limit",
        values.get("EVE_SENTRY_SERVER_HOT_REPORT_LIMIT", ""),
    )
    _append_option(argv, "--auth-mode", values.get("EVE_SENTRY_SERVER_AUTH_MODE", ""))
    _append_option(
        argv,
        "--auth-bootstrap-admin",
        values.get("EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_ADMIN", ""),
    )
    _append_option(
        argv,
        "--auth-bootstrap-password-file",
        values.get("EVE_SENTRY_SERVER_AUTH_BOOTSTRAP_PASSWORD_FILE", ""),
    )
    _append_option(
        argv,
        "--seat-integration-token",
        values.get("EVE_SENTRY_SERVER_SEAT_INTEGRATION_TOKEN", ""),
    )
    _append_option(
        argv,
        "--seat-auth-mode",
        values.get("EVE_SENTRY_SERVER_SEAT_AUTH_MODE", ""),
    )
    if _env_flag(values.get("EVE_SENTRY_SERVER_ALLOW_ALERT_CONSUMPTION")):
        argv.append("--allow-alert-consumption")
    if _env_flag(values.get("EVE_SENTRY_SERVER_QQ_BOT_ENABLED")):
        argv.append("--enable-qq-bot")
    _append_option(
        argv,
        "--qq-bot-source",
        values.get("EVE_SENTRY_SERVER_QQ_BOT_SOURCE", ""),
    )
    _append_option(argv, "--config", values.get("EVE_SENTRY_SERVER_CONFIG", ""))
    _append_option(
        argv,
        "--map-config",
        values.get("EVE_SENTRY_SERVER_MAP_CONFIG", ""),
    )
    _append_option(
        argv,
        "--map-source",
        values.get("EVE_SENTRY_SERVER_MAP_SOURCE", ""),
    )
    _append_option(
        argv,
        "--map-sde-path",
        values.get("EVE_SENTRY_SERVER_MAP_SDE_PATH", ""),
    )
    for region_id in _split_csv(values.get("EVE_SENTRY_SERVER_MAP_REGION_IDS", "")):
        argv.extend(["--map-region", region_id])
    for system_id in _split_csv(values.get("EVE_SENTRY_SERVER_MAP_SYSTEM_IDS", "")):
        argv.extend(["--map-system", system_id])
    if _env_flag(values.get("EVE_SENTRY_SERVER_MAP_REFRESH_ON_START")):
        argv.append("--map-refresh-on-start")

    if _env_flag(values.get("EVE_SENTRY_SERVER_ENABLE_ESI")):
        argv.append("--enable-esi")
    _append_option(argv, "--esi-cache", values.get("EVE_SENTRY_SERVER_ESI_CACHE", ""))
    _append_option(argv, "--esi-backend", values.get("EVE_SENTRY_SERVER_ESI_BACKEND", ""))
    _append_option(argv, "--esi-gateway-url", values.get("EVE_SENTRY_SERVER_ESI_GATEWAY_URL", ""))
    _append_option(argv, "--esi-gateway-token", values.get("EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN", ""))
    _append_option(argv, "--esi-remote-timeout", values.get("EVE_SENTRY_SERVER_ESI_REMOTE_TIMEOUT", ""))
    if _env_flag(values.get("EVE_SENTRY_SERVER_ESI_NO_LOCAL_FALLBACK")):
        argv.append("--esi-no-local-fallback")

    return argv


def main(argv: list[str] | None = None) -> int:
    combined = build_server_argv()
    if argv:
        combined.extend(argv)
    return server_main.main(combined)


def _append_option(argv: list[str], flag: str, value: str) -> None:
    text = str(value or "").strip()
    if text:
        argv.extend([flag, text])


def _env_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str) -> list[str]:
    text = str(value or "").replace("\n", ",")
    items: list[str] = []
    seen: set[str] = set()
    for raw in text.split(","):
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            items.append(item)
    return items


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
