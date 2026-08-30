"""Incremental EVE game-log connection monitoring.

Game logs are process/session logs rather than channel logs.  This module only
keeps a small tail cursor for the newest files and never replays the full
Gamelogs history.  A log instance is identified by its filename stem; the
optional trailing numeric token is also exposed so callers can correlate it
with a character/client ID when the launcher includes one.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_GAMELOG_TAIL_BYTES = 128 * 1024
DEFAULT_GAMELOG_RECENT_SECONDS = 24 * 60 * 60
_TIMESTAMP_RE = re.compile(r"(?P<date>\d{8})[_-](?P<time>\d{6})")
_TRAILING_ID_RE = re.compile(r"(?:^|[_-])(?P<id>\d+)$")

DISCONNECT_PATTERNS = (
    "你的计算机已与EVE Online服务器断开网络通信",
    "无法连接到指定地址",
    "服务器当前不接受连接",
    "连接丢失",
    "与服务器的连接已被关闭",
    "与服务器的连接已关闭",
    "与服务器的连接已断开",
    "the connection to the server has been closed",
    "disconnected from the server",
    "connection lost",
    "connection to the server was lost",
)
CONNECT_PATTERNS = (
    "与服务器的连接已建立",
    "已连接到服务器",
    "connected to the server",
    "connection established",
    "connection to the server has been established",
    "login successful",
)


@dataclass(frozen=True)
class GameConnectionEvent:
    """A newly observed game-server connection transition."""

    target_key: str
    client_id: str
    log_id: str
    state: str
    message: str
    occurred_at: str = ""


@dataclass
class _Cursor:
    signature: tuple[int, int]
    offset: int
    initialized: bool = False


class GameConnectionLogWatcher:
    """Read only appended lines from the newest per-client game log."""

    def __init__(
        self,
        log_dir: str | Path,
        *,
        tail_bytes: int = DEFAULT_GAMELOG_TAIL_BYTES,
        recent_seconds: float = DEFAULT_GAMELOG_RECENT_SECONDS,
        max_candidates: int = 32,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.tail_bytes = max(4096, int(tail_bytes))
        self.recent_seconds = max(60.0, float(recent_seconds))
        self.max_candidates = max(1, int(max_candidates))
        self._assignments: dict[str, str] = {}
        self._cursors: dict[str, _Cursor] = {}
        self._seen_events: set[tuple[str, str]] = set()

    def poll(self, targets: Iterable[dict[str, Any]]) -> list[GameConnectionEvent]:
        """Return new transitions for the supplied monitored targets."""
        target_list = [target for target in targets if isinstance(target, dict)]
        if not target_list:
            return []
        paths = self._discover_latest_paths()
        by_name = {path.name.casefold(): path for path in paths}
        active_names: set[str] = set()
        events: list[GameConnectionEvent] = []
        available = list(paths)
        for target in target_list:
            key = str(target.get("key") or target.get("client_id") or "").strip()
            client_id = str(target.get("client_id") or key).strip()
            if not key:
                continue
            assigned_name = self._assignments.get(key, "")
            path = by_name.get(assigned_name.casefold()) if assigned_name else None
            candidate = self._match_path(target, available)
            if (
                path is not None
                and candidate is not None
                and candidate.name.casefold() != path.name.casefold()
                and self._should_rebind(target, path, candidate)
            ):
                path = candidate
            if path is None:
                path = candidate
                if path is not None:
                    self._assignments[key] = path.name
            if path is None:
                continue
            active_names.add(path.name.casefold())
            available = [item for item in available if item.name.casefold() != path.name.casefold()]
            events.extend(self._read_path(path, key, client_id))

        # Forget sessions that are no longer among the newest files.  This lets
        # a restarted EVE process acquire its new log instance cleanly.
        for key, name in list(self._assignments.items()):
            if name.casefold() not in active_names and name.casefold() not in by_name:
                self._assignments.pop(key, None)
        return events

    def _should_rebind(self, target: dict[str, Any], current: Path, candidate: Path) -> bool:
        """Switch to a newer session only when its identity is attributable."""
        wanted_ids = {
            str(value).strip()
            for value in (
                target.get("game_log_id"),
                target.get("character_id"),
                target.get("user_id"),
            )
            if str(value or "").strip()
        }
        if wanted_ids:
            return True
        process_started = target.get("process_started_at") or _window_process_started_at(target)
        try:
            started = float(process_started)
        except (TypeError, ValueError):
            started = 0.0
        if not started:
            return False
        current_stamp = game_log_timestamp(current)
        candidate_stamp = game_log_timestamp(candidate)
        if current_stamp is None or candidate_stamp is None:
            return False
        return abs(candidate_stamp - started) < abs(
            current_stamp - started
        )

    def _discover_latest_paths(self) -> list[Path]:
        try:
            with os.scandir(self.log_dir) as entries:
                candidates: list[tuple[int, str, Path]] = []
                cutoff = time.time() - self.recent_seconds
                for entry in entries:
                    if not entry.name.casefold().endswith(".txt"):
                        continue
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    if not entry.is_file() or stat.st_mtime < cutoff:
                        continue
                    candidates.append((int(stat.st_mtime_ns), entry.name.casefold(), Path(entry.path)))
        except OSError:
            return []
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in candidates[: self.max_candidates]]

    def _match_path(self, target: dict[str, Any], paths: list[Path]) -> Path | None:
        if not paths:
            return None
        wanted_ids = {
            str(value).strip()
            for value in (
                target.get("game_log_id"),
                target.get("character_id"),
                target.get("user_id"),
            )
            if str(value or "").strip()
        }
        for path in paths:
            log_id = game_log_id(path)
            trailing_id = trailing_game_log_id(path)
            if log_id in wanted_ids or trailing_id in wanted_ids:
                return path

        process_started = target.get("process_started_at")
        if not process_started:
            process_started = _window_process_started_at(target)
        if process_started:
            try:
                started = float(process_started)
            except (TypeError, ValueError):
                started = 0.0
            if started:
                scored = []
                for path in paths:
                    stamp = game_log_timestamp(path)
                    if stamp is not None:
                        scored.append((abs(stamp - started), path))
                if scored:
                    scored.sort(key=lambda item: item[0])
                    return scored[0][1]
        return paths[0]

    def _read_path(self, path: Path, target_key: str, client_id: str) -> list[GameConnectionEvent]:
        try:
            stat = path.stat()
        except OSError:
            return []
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        log_id = game_log_id(path)
        cursor = self._cursors.get(log_id)
        if cursor is None or stat.st_size < cursor.offset:
            start = max(0, int(stat.st_size) - self.tail_bytes)
            cursor = _Cursor(signature=signature, offset=start, initialized=False)
            self._cursors[log_id] = cursor
        elif signature == cursor.signature:
            return []
        try:
            with path.open("rb") as stream:
                stream.seek(cursor.offset)
                raw = stream.read()
        except OSError:
            return []
        cursor.offset = int(stat.st_size)
        cursor.signature = signature
        if not raw:
            return []
        text = raw.decode("utf-8", errors="replace")
        events: list[GameConnectionEvent] = []
        for line in text.splitlines():
            state = connection_state_from_line(line)
            if state is None:
                continue
            event_key = (log_id, line.strip())
            if event_key in self._seen_events:
                continue
            self._seen_events.add(event_key)
            events.append(
                GameConnectionEvent(
                    target_key=target_key,
                    client_id=client_id,
                    log_id=log_id,
                    state=state,
                    message=line.strip(),
                    occurred_at=_line_timestamp(line),
                )
            )
        cursor.initialized = True
        if len(self._seen_events) > 2048:
            self._seen_events = set(list(self._seen_events)[-1024:])
        return events


def game_log_id(path: str | Path) -> str:
    """Return the stable session ID represented by a Gamelog filename."""
    return Path(path).stem.strip()


def trailing_game_log_id(path: str | Path) -> str:
    match = _TRAILING_ID_RE.search(game_log_id(path))
    return match.group("id") if match else ""


def game_log_timestamp(path: str | Path) -> float | None:
    match = _TIMESTAMP_RE.search(game_log_id(path))
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('date')}_{match.group('time')}", "%Y%m%d_%H%M%S"
        ).timestamp()
    except ValueError:
        return None


def connection_state_from_line(line: str) -> str | None:
    text = " ".join(str(line or "").casefold().split())
    if any(pattern.casefold() in text for pattern in DISCONNECT_PATTERNS):
        return "offline"
    if any(pattern.casefold() in text for pattern in CONNECT_PATTERNS):
        return "online"
    return None


def _line_timestamp(line: str) -> str:
    match = re.search(r"\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}:\d{2}", line)
    return match.group(0) if match else ""


def _window_process_started_at(target: dict[str, Any]) -> float:
    """Best-effort process start time used to bind a Gamelog to a window."""
    hwnd = target.get("hwnd")
    if hwnd in {None, ""}:
        window = target.get("window")
        hwnd = window.get("hwnd") if isinstance(window, dict) else None
    try:
        import psutil
        import win32process

        _thread_id, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        return float(psutil.Process(pid).create_time())
    except Exception:
        return 0.0
