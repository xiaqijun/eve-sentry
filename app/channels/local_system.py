"""Detect the current solar system from EVE local-channel chatlogs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.channels.log_watcher import (
    channel_name_from_path,
    detect_encoding,
    normalize_channel_name,
)

LOCAL_SYSTEM_RE = re.compile(
    r"(?:频道更换为|频道已更换为|Channel\s+changed\s+to)\s*"
    r"(?:本地|Local)\s*[:：]\s*"
    r"(?P<system>[A-Za-z0-9][A-Za-z0-9-]{1,15})\*?",
    re.IGNORECASE,
)
LOCAL_CHANNELS = {"local", "本地"}
LISTENER_RE = re.compile(
    r"^\s*Listener\s*:\s*(?P<character>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LocalSystemDetection:
    """A local-channel system switch found in a chatlog."""

    system_name: str
    path: Path
    line: str
    character_name: str = ""


def parse_local_system_line(line: str) -> str:
    """Return the system name from an EVE local-channel switch line."""
    match = LOCAL_SYSTEM_RE.search(str(line or ""))
    if not match:
        return ""
    return match.group("system").strip().rstrip("*")


def parse_listener_line(line: str) -> str:
    """Return the character name from an EVE chatlog Listener header."""
    match = LISTENER_RE.match(str(line or ""))
    if not match:
        return ""
    return match.group("character").strip()


def find_latest_local_system(
    log_dir: str | Path,
    character_name: str = "",
) -> LocalSystemDetection | None:
    """Return the newest local-channel system switch from EVE Chatlogs."""
    root = Path(log_dir)
    if not root.exists():
        return None

    paths = [path for path in root.glob("*.txt") if path.is_file()]
    if not paths:
        return None

    local_paths = [
        path
        for path in paths
        if normalize_channel_name(channel_name_from_path(path)) in LOCAL_CHANNELS
    ]
    candidates = local_paths or paths
    candidates.sort(key=lambda item: (item.stat().st_mtime, item.name), reverse=True)

    requested_character = str(character_name or "").strip().casefold()
    for path in candidates:
        detection = _find_latest_local_system_in_file(path)
        if detection is None:
            continue
        if requested_character and detection.character_name.casefold() != requested_character:
            continue
        return detection
    return None


def _find_latest_local_system_in_file(path: Path) -> LocalSystemDetection | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None

    encoding = detect_encoding(data)
    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")

    lines = text.splitlines()
    character_name = ""
    for line in lines:
        character_name = parse_listener_line(line)
        if character_name:
            break

    for line in reversed(lines):
        system_name = parse_local_system_line(line)
        if system_name:
            return LocalSystemDetection(
                system_name=system_name,
                path=path,
                line=line,
                character_name=character_name,
            )
    return None
