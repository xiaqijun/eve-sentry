"""Watch EVE chatlog files with byte-offset resume support."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_CHATLOG_DIR = Path.home() / "Documents" / "EVE" / "logs" / "Chatlogs"
CHANNEL_SUFFIX_RE = re.compile(r"(?:[_-]\d{8})?(?:[_-]\d{6})(?:[_-]\d+)?$")


@dataclass(frozen=True)
class ChatLogLine:
    """One newly read chatlog line."""

    path: Path
    channel: str
    text: str


class OffsetStore:
    """JSON-backed byte offsets for watched chatlog files."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._offsets: dict[str, int] = self._load()

    def get(self, file_path: Path) -> int:
        """Return the remembered byte offset for a file."""
        return int(self._offsets.get(str(file_path.resolve()), 0))

    def set(self, file_path: Path, offset: int) -> None:
        """Remember the byte offset for a file."""
        self._offsets[str(file_path.resolve())] = max(0, int(offset))

    def save(self) -> None:
        """Persist offsets to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._offsets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> dict[str, int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in raw.items():
            try:
                result[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return result


class ChatLogWatcher:
    """Discover and tail EVE chatlog files."""

    def __init__(
        self,
        log_dir: str | Path = DEFAULT_CHATLOG_DIR,
        channels: Iterable[str] | None = None,
        state_path: str | Path = "channel_offsets.json",
    ) -> None:
        self.log_dir = Path(log_dir)
        self.channels = [item.casefold() for item in (channels or []) if item]
        self.state = OffsetStore(state_path)

    def seed_to_end(self) -> None:
        """Mark current files as consumed without returning their content."""
        for path in self.discover_files():
            self.state.set(path, path.stat().st_size)
        self.state.save()

    def poll_lines(self) -> list[ChatLogLine]:
        """Return newly appended lines from matching chatlog files."""
        lines: list[ChatLogLine] = []
        for path in self.discover_files():
            lines.extend(self._read_new_lines(path))
        if lines:
            self.state.save()
        return lines

    def discover_files(self) -> list[Path]:
        """Return matching chatlog text files sorted by mtime then name."""
        if not self.log_dir.exists():
            return []
        files = [
            path
            for path in self.log_dir.glob("*.txt")
            if path.is_file() and self._matches_channel(path)
        ]
        files.sort(key=lambda item: (item.stat().st_mtime, item.name))
        return files

    def _read_new_lines(self, path: Path) -> list[ChatLogLine]:
        size = path.stat().st_size
        offset = self.state.get(path)
        if offset > size:
            offset = 0
        if offset == size:
            return []

        data = path.read_bytes()
        chunk = data[offset:]
        encoding = detect_encoding(data)
        text = chunk.decode(encoding, errors="replace")
        self.state.set(path, size)
        channel = channel_name_from_path(path)
        return [
            ChatLogLine(path=path, channel=channel, text=line)
            for line in text.splitlines()
            if line.strip()
        ]

    def _matches_channel(self, path: Path) -> bool:
        if not self.channels:
            return True
        channel = channel_name_from_path(path).casefold()
        return any(item in channel for item in self.channels)


def detect_encoding(data: bytes) -> str:
    """Detect the common encodings used by EVE chatlogs."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"
    sample = data[:200]
    if sample.count(b"\x00") > max(2, len(sample) // 8):
        return "utf-16"
    return "utf-8-sig"


def channel_name_from_path(path: Path) -> str:
    """Derive a stable channel name from an EVE chatlog filename."""
    stem = path.stem.strip()
    cleaned = CHANNEL_SUFFIX_RE.sub("", stem).strip(" _-")
    return cleaned or stem
