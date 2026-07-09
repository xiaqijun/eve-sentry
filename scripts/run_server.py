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
    _append_option(argv, "--db", values.get("EVE_SENTRY_SERVER_DB", ""))
    _append_option(
        argv,
        "--postgres-dsn",
        values.get("EVE_SENTRY_SERVER_POSTGRES_DSN", ""),
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
    _append_option(
        argv,
        "--esi-client-id",
        values.get("EVE_SENTRY_SERVER_ESI_CLIENT_ID", ""),
    )
    _append_option(
        argv,
        "--esi-redirect-uri",
        values.get("EVE_SENTRY_SERVER_ESI_REDIRECT_URI", ""),
    )
    _append_option(
        argv,
        "--esi-token-file",
        values.get("EVE_SENTRY_SERVER_ESI_TOKEN_FILE", ""),
    )
    _append_option(
        argv,
        "--esi-token-storage",
        values.get("EVE_SENTRY_SERVER_ESI_TOKEN_STORAGE", ""),
    )
    if _env_flag(values.get("EVE_SENTRY_SERVER_ESI_LOGIN")):
        argv.append("--esi-login")
    if _env_flag(values.get("EVE_SENTRY_SERVER_ESI_LOGIN_ONLY")):
        argv.append("--esi-login-only")
    _append_option(
        argv,
        "--esi-login-timeout",
        values.get("EVE_SENTRY_SERVER_ESI_LOGIN_TIMEOUT", ""),
    )
    if _env_flag(values.get("EVE_SENTRY_SERVER_ESI_NO_BROWSER")):
        argv.append("--esi-no-browser")
    for scope in _split_scopes(values.get("EVE_SENTRY_SERVER_ESI_SCOPES", "")):
        argv.extend(["--esi-scope", scope])

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


def _split_scopes(value: str) -> list[str]:
    text = str(value or "").replace(",", " ")
    scopes: list[str] = []
    seen: set[str] = set()
    for item in text.split():
        scope = item.strip()
        if scope and scope not in seen:
            seen.add(scope)
            scopes.append(scope)
    return scopes


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
