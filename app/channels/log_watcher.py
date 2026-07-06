"""Watch EVE chatlog files with byte-offset resume support."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable


DEFAULT_CHATLOG_DIR = Path.home() / "Documents" / "EVE" / "logs" / "Chatlogs"
CHANNEL_SUFFIX_RE = re.compile(r"(?:[_-]\d{8})?(?:[_-]\d{6})(?:[_-]\d+)?$")
WILDCARD_CHARS = frozenset("*?")


@dataclass(frozen=True)
class ChatLogLine:
    """One newly read chatlog line."""

    path: Path
    channel: str
    text: str
    end_offset: int


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
        self.channels = normalize_channel_filters(channels or [])
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
        return lines

    def commit_line(self, line: ChatLogLine) -> None:
        """Persist the offset for a line after it has been handled."""
        if line.end_offset > self.state.get(line.path):
            self.state.set(line.path, line.end_offset)
            self.state.save()

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
        encoding = detect_encoding(data)
        channel = channel_name_from_path(path)
        newline = newline_sequence(data, encoding)
        cursor = offset
        lines: list[ChatLogLine] = []
        blank_offset = offset
        while cursor < size:
            newline_at = data.find(newline, cursor)
            if newline_at < 0:
                break
            end_offset = newline_at + len(newline)
            raw_line = data[cursor:end_offset]
            text = raw_line.decode(
                line_decode_encoding(data, cursor, encoding),
                errors="replace",
            ).lstrip("\ufeff")
            if text.strip():
                lines.append(
                    ChatLogLine(
                        path=path,
                        channel=channel,
                        text=text.rstrip("\r\n"),
                        end_offset=end_offset,
                    )
                )
            else:
                blank_offset = end_offset
            cursor = end_offset
        if not lines and blank_offset > offset:
            self.state.set(path, blank_offset)
            self.state.save()
        return lines

    def _matches_channel(self, path: Path) -> bool:
        if not self.channels:
            return True
        channel = normalize_channel_name(channel_name_from_path(path))
        return any(channel_filter_matches(item, channel) for item in self.channels)


def normalize_channel_name(value: str) -> str:
    """Normalize a channel name or configured filter for stable matching."""
    return " ".join(str(value or "").strip().casefold().split())


def normalize_channel_filters(channels: Iterable[str]) -> list[str]:
    """Return non-empty normalized channel filters."""
    result: list[str] = []
    for item in channels:
        normalized = normalize_channel_name(str(item))
        if normalized:
            result.append(normalized)
    return result


def channel_filter_matches(channel_filter: str, channel: str) -> bool:
    """Match exact channel names unless a filter explicitly uses wildcards."""
    if any(char in channel_filter for char in WILDCARD_CHARS):
        return fnmatchcase(channel, channel_filter)
    return channel == channel_filter


def newline_sequence(data: bytes, encoding: str) -> bytes:
    """Return the byte newline sequence for the detected text encoding."""
    if encoding == "utf-16":
        if data.startswith(b"\xfe\xff"):
            return b"\x00\n"
        return b"\n\x00"
    return b"\n"


def line_decode_encoding(data: bytes, offset: int, encoding: str) -> str:
    """Return a codec that can decode an individual line from a byte offset."""
    if encoding != "utf-16":
        return encoding
    if offset == 0 and (data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff")):
        return "utf-16"
    if data.startswith(b"\xfe\xff"):
        return "utf-16-be"
    return "utf-16-le"


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
