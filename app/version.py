"""Application version and release channel defaults."""

from __future__ import annotations

import os


APP_VERSION = "1.0.58"
DEFAULT_UPDATE_MANIFEST_URL = (
    "https://evesentrydownload.kisectool.com/latest.json"
)


def current_version() -> str:
    """Return the packaged client version, allowing explicit test overrides."""
    return str(os.environ.get("EVE_SENTRY_CLIENT_VERSION") or APP_VERSION).strip()


def update_manifest_url() -> str:
    """Return the release manifest URL used by the desktop updater."""
    return str(
        os.environ.get("EVE_SENTRY_UPDATE_MANIFEST_URL")
        or DEFAULT_UPDATE_MANIFEST_URL
    ).strip()
