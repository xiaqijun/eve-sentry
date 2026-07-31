"""Persistent identities for EVE Sentry runtime clients."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def default_client_identity_path() -> Path:
    """Return the per-installation identity file path."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "EVE Sentry" / "client_identity.json"
    return Path.home() / ".eve-sentry" / "client_identity.json"


def _read_installation_id(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = str(payload.get("installation_id") or "").strip().lower()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return ""
    return parsed.hex


def load_or_create_installation_id(path: str | Path | None = None) -> str:
    """Load the installation UUID, creating it once when needed.

    The exclusive file create keeps concurrently starting detector and alert
    processes on the same installation from claiming different identities.
    """
    identity_path = Path(path) if path else default_client_identity_path()
    identity_path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(20):
        existing = _read_installation_id(identity_path)
        if existing:
            return existing

        installation_id = uuid.uuid4().hex
        payload = json.dumps(
            {"version": 1, "installation_id": installation_id},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        try:
            descriptor = os.open(
                identity_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            # Another runtime may be writing the file. Give it a brief chance
            # to finish before reading it again.
            time.sleep(0.01)
            continue
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
        except Exception:
            try:
                identity_path.unlink()
            except OSError:
                pass
            raise
        return installation_id

    raise RuntimeError(f"could not load or create client identity: {identity_path}")


def persistent_client_id(client_type: str, path: str | Path | None = None) -> str:
    """Return a stable ID for one logical client type."""
    prefix = str(client_type or "client").strip()
    if not prefix:
        prefix = "client"
    if not prefix.endswith("-client"):
        prefix = f"{prefix}-client"
    return f"{prefix}:{load_or_create_installation_id(path)}"
