"""Watch EVE chatlog files with byte-offset resume support."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable


def _windows_documents_dir() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Personal")
    except (ImportError, OSError):
        return None
    expanded = os.path.expandvars(str(value)).strip()
    return Path(expanded) if expanded else None


def _default_chatlog_candidates() -> list[Path]:
    documents_dirs: list[Path] = []
    windows_documents = _windows_documents_dir()
    if windows_documents is not None:
        documents_dirs.append(windows_documents)
    documents_dirs.append(Path.home() / "Documents")

    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        documents_dirs.append(Path(user_profile) / "Documents")
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(name, "").strip()
        if root:
            documents_dirs.append(Path(root) / "Documents")

    candidates: list[Path] = []
    seen: set[str] = set()
    for documents_dir in documents_dirs:
        candidate = documents_dir / "EVE" / "logs" / "Chatlogs"
        marker = os.path.normcase(str(candidate))
        if marker in seen:
            continue
        seen.add(marker)
        candidates.append(candidate)
    return candidates


def resolve_chatlog_dir(preferred: str | Path | None = None) -> Path:
    """Return the active EVE Chatlogs directory from Windows path candidates."""
    candidates: list[Path] = []
    if preferred is not None and str(preferred).strip():
        candidates.append(Path(os.path.expandvars(str(preferred))).expanduser())
    candidates.extend(_default_chatlog_candidates())

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        marker = os.path.normcase(str(candidate))
        if marker in seen:
            continue
        seen.add(marker)
        unique_candidates.append(candidate)

    active_candidates: list[tuple[float, Path]] = []
    for candidate in unique_candidates:
        try:
            latest_mtime = max(
                (path.stat().st_mtime for path in candidate.glob("*.txt") if path.is_file()),
                default=None,
            )
        except OSError:
            continue
        if latest_mtime is not None:
            active_candidates.append((latest_mtime, candidate))
    if active_candidates:
        return max(active_candidates, key=lambda item: item[0])[1]

    for candidate in unique_candidates:
        if candidate.is_dir():
            return candidate
    if unique_candidates:
        return unique_candidates[0]
    return Path.home() / "Documents" / "EVE" / "logs" / "Chatlogs"


DEFAULT_CHATLOG_DIR = resolve_chatlog_dir()
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

    def has(self, file_path: Path) -> bool:
        """Return whether a file already has a remembered offset."""
        return str(file_path.resolve()) in self._offsets

    def set(self, file_path: Path, offset: int) -> None:
        """Remember the byte offset for a file."""
        self._offsets[str(file_path.resolve())] = max(0, int(offset))

    def save(self) -> None:
        """Persist offsets to disk with an atomic replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        temp_path.write_text(
            json.dumps(self._offsets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)

    def _load(self) -> dict[str, int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            self._backup_invalid_state()
            return {}
        except OSError:
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

    def _backup_invalid_state(self) -> None:
        backup_path = self.path.with_name(f"{self.path.name}.invalid")
        try:
            self.path.replace(backup_path)
        except OSError:
            pass


class ChatLogWatcher:
    """Discover and tail EVE chatlog files."""

    def __init__(
        self,
        log_dir: str | Path = DEFAULT_CHATLOG_DIR,
        channels: Iterable[str] | None = None,
        state_path: str | Path = "channel_offsets.json",
        start_at_end_for_new_files: bool = False,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.channels = normalize_channel_filters(channels or [])
        self.state = OffsetStore(state_path)
        self.start_at_end_for_new_files = bool(start_at_end_for_new_files)

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
        """Return the newest matching chatlog text file for each channel."""
        if not self.log_dir.exists():
            return []
        latest_by_channel: dict[str, tuple[float, str, Path]] = {}
        for path in self.log_dir.glob("*.txt"):
            if not path.is_file() or not self._matches_channel(path):
                continue
            channel = normalize_channel_name(channel_name_from_path(path))
            marker = (path.stat().st_mtime, path.name, path)
            current = latest_by_channel.get(channel)
            if current is None or marker[:2] > current[:2]:
                latest_by_channel[channel] = marker
        files = [item[2] for item in latest_by_channel.values()]
        files.sort(key=lambda item: (item.stat().st_mtime, item.name))
        return files

    def _read_new_lines(self, path: Path) -> list[ChatLogLine]:
        size = path.stat().st_size
        known_offset = self.state.has(path)
        offset = self.state.get(path)
        if offset > size:
            offset = size if self.start_at_end_for_new_files else 0
            self.state.set(path, offset)
            self.state.save()
        if not known_offset and self.start_at_end_for_new_files:
            self.state.set(path, size)
            self.state.save()
            return []
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
