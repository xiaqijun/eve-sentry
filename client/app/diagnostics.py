"""Rolling client logs and privacy-safe diagnostic bundle export."""

from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_ -]?key|token|secret)(\s*[:=]\s*)([^\s,;]+)"
)


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "EVE Sentry" if base else Path.home() / ".eve-sentry"
    return root / "logs" / "client.log"


def configure_client_logging(level: int = logging.INFO) -> Path:
    """Configure stderr plus bounded rotating log files once."""
    log_path = default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(handler, "_eve_sentry_file", False) for handler in root.handlers):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler._eve_sentry_file = True
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if not root.handlers or not any(
        isinstance(handler, logging.StreamHandler)
        and not getattr(handler, "_eve_sentry_file", False)
        for handler in root.handlers
    ):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    return log_path


def export_diagnostic_bundle(
    target: Path,
    diagnostics: dict[str, Any],
    log_path: Path | None = None,
) -> Path:
    """Write runtime metadata and redacted rolling logs to a zip archive."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    active_log = Path(log_path or default_log_path())
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "diagnostics.json",
            json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str),
        )
        for candidate in [active_log, *sorted(active_log.parent.glob("client.log.*"))]:
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            archive.writestr(f"logs/{candidate.name}", redact_secrets(text))
    return destination


def redact_secrets(text: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2<redacted>", str(text))
