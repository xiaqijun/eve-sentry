"""Incremental EVE Listener discovery with protected local state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.channels.local_system import LOCAL_CHANNELS, parse_listener_line
from app.channels.log_watcher import (
    channel_name_from_path,
    detect_encoding,
    normalize_channel_name,
)
from app.esi.sso import default_token_protector, token_protector_from_name
from app.intel_client import INVALID_API_KEY_MESSAGE, is_valid_api_key


DEFAULT_INITIAL_SCAN_LIMIT = 64
MAX_TRACKED_FILES = 500
RECENT_LOCAL_LOG_SECONDS = 24 * 60 * 60
TRAILING_CHARACTER_ID_RE = re.compile(
    r"(?:^|[_-])(?P<character_id>\d+)$"
)


def default_client_auth_state_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "EVE Sentry" / "client_auth.json"
    return Path.home() / ".eve-sentry" / "client_auth.json"


@dataclass(frozen=True)
class IdentityScanResult:
    """One incremental scan result."""

    characters: list[str]
    pending_characters: list[str]
    pending_files: list[str]
    processed_count: int
    initial_scan: bool
    key_validated: bool
    identity_verified: bool
    character_ids: list[int] = field(default_factory=list)
    pending_character_ids: list[int] = field(default_factory=list)


class ClientAuthStateStore:
    """Persist API credentials and Listener history with Windows DPAPI."""

    def __init__(self, path: str | Path | None = None, protector: Any | None = None):
        self.path = Path(path) if path else default_client_auth_state_path()
        self.protector = protector if protector is not None else default_token_protector()

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.empty_state()
        if not isinstance(payload, dict):
            return self.empty_state()
        if payload.get("protected") is True:
            payload = self._unprotect(payload)
            if payload is None:
                return self.empty_state()
        state = self.empty_state()
        state.update(payload)
        state["processed_files"] = _clean_file_names(
            state.get("processed_files", [])
        )[-MAX_TRACKED_FILES:]
        state["characters"] = _clean_names(state.get("characters", []))
        state["pending_characters"] = _clean_names(state.get("pending_characters", []))
        state["character_identities"] = _clean_character_identities(
            state.get("character_identities", [])
        )
        processed_character_ids = [
            character_id
            for name in state["processed_files"]
            if (character_id := _character_id_from_log_path(Path(name))) is not None
        ]
        state["listener_character_ids"] = _clean_character_ids(
            list(state.get("listener_character_ids", []))
            + processed_character_ids
        )
        state["pending_character_ids"] = _clean_character_ids(
            state.get("pending_character_ids", [])
        )
        for obsolete_key in (
            "listener_cursor",
            "listener_directory_mtime_ns",
            "listener_queue",
            "listener_pending_files",
        ):
            state.pop(obsolete_key, None)
        return state

    def save(self, state: dict[str, Any]) -> None:
        payload = dict(state)
        if self.protector is not None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            encrypted = self.protector.protect(raw)
            payload = {
                "version": 1,
                "protected": True,
                "provider": self.protector.name,
                "payload": base64.b64encode(encrypted).decode("ascii"),
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def api_key(self) -> str:
        api_key = str(self.load().get("api_key") or "").strip()
        return api_key if is_valid_api_key(api_key, allow_empty=True) else ""

    def set_api_key(self, api_key: str) -> bool:
        api_key = str(api_key or "").strip()
        if not is_valid_api_key(api_key, allow_empty=True):
            raise ValueError(INVALID_API_KEY_MESSAGE)
        state = self.load()
        previous = str(state.get("api_key") or "").strip()
        if previous == api_key:
            return False
        state["api_key"] = api_key
        state["key_fingerprint"] = _key_fingerprint(api_key)
        state["key_validated"] = False
        state["identity_verified"] = False
        state["pending_characters"] = _clean_names(
            state.get("characters", [])
        )
        state["pending_character_ids"] = _clean_character_ids(
            state.get("listener_character_ids", [])
        )
        self.save(state)
        return True

    def remember_character_identities(
        self,
        characters: list[dict[str, Any]],
    ) -> None:
        """Merge server-resolved EVE character IDs into protected local state."""
        resolved = _clean_character_identities(characters)
        if not resolved:
            return
        state = self.load()
        identities = {
            str(item["character_name"]).casefold(): item
            for item in state.get("character_identities", [])
        }
        for item in resolved:
            identities[str(item["character_name"]).casefold()] = item
        state["character_identities"] = list(identities.values())
        self.save(state)

    def empty_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "api_key": "",
            "key_fingerprint": "",
            "initialized": False,
            "key_validated": False,
            "identity_verified": False,
            "processed_files": [],
            "characters": [],
            "pending_characters": [],
            "character_identities": [],
            "listener_character_ids": [],
            "pending_character_ids": [],
        }

    def _unprotect(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        protector = self.protector or token_protector_from_name(
            str(payload.get("provider") or "")
        )
        if protector is None:
            return None
        try:
            encrypted = base64.b64decode(str(payload.get("payload") or ""), validate=True)
            raw = protector.unprotect(encrypted)
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None


class EveIdentityLogScanner:
    """Discover Listener headers incrementally without replaying all history."""

    def __init__(
        self,
        log_dir: str | Path,
        state_store: ClientAuthStateStore,
        *,
        initial_scan_limit: int = DEFAULT_INITIAL_SCAN_LIMIT,
        rescan_interval: float = 30.0,
    ):
        self.log_dir = Path(log_dir)
        self.state_store = state_store
        self.initial_scan_limit = max(1, int(initial_scan_limit))
        self.rescan_interval = max(30.0, float(rescan_interval))
        self._last_directory_mtime_ns: int | None = None
        self._next_forced_discovery = 0.0
        self._pending_files: dict[str, dict[str, int]] = {}

    def scan(self, api_key: str) -> IdentityScanResult:
        state = self.state_store.load()
        api_key = str(api_key or "").strip()
        if state.get("key_fingerprint") != _key_fingerprint(api_key):
            self.state_store.set_api_key(api_key)
            state = self.state_store.load()

        initial_scan = not bool(state.get("initialized"))
        processed_files = _clean_file_names(state.get("processed_files", []))
        processed = set(processed_files)
        characters = _clean_names(state.get("characters", []))
        previous_character_keys = {item.casefold() for item in characters}
        character_keys = {item.casefold() for item in characters}
        character_ids = _clean_character_ids(
            state.get("listener_character_ids", [])
        )
        previous_character_ids = set(character_ids)
        character_id_keys = set(character_ids)
        processed_count = 0

        directory_mtime_ns = _directory_mtime_ns(self.log_dir)
        now = time.monotonic()
        should_discover = (
            self._last_directory_mtime_ns is None
            or directory_mtime_ns != self._last_directory_mtime_ns
            or now >= self._next_forced_discovery
        )
        candidates: list[Path] = []
        if should_discover:
            entries = self._discover_entries()
            candidates.extend(
                path
                for _marker, path in entries
                if path.name not in processed
                and path.name not in self._pending_files
            )
            candidates = candidates[-self.initial_scan_limit:]
            self._last_directory_mtime_ns = directory_mtime_ns
            self._next_forced_discovery = now + self.rescan_interval

        for name, previous_signature in list(self._pending_files.items()):
            path = self.log_dir / name
            signature = _file_signature(path)
            if signature is None:
                self._pending_files.pop(name, None)
            elif signature != previous_signature:
                candidates.append(path)
                self._pending_files.pop(name, None)

        unique_candidates = {
            path.name.casefold(): path for path in candidates
        }
        for path in list(unique_candidates.values())[-self.initial_scan_limit:]:
            name = path.name
            character_id = _character_id_from_log_path(path)
            listener = "" if character_id is not None else _listener_from_file(path)
            if character_id is None and not listener:
                signature = _file_signature(path)
                if signature is not None:
                    self._pending_files[name] = signature
                continue
            processed.add(name)
            processed_files.append(name)
            processed_count += 1
            if listener and listener.casefold() not in character_keys:
                character_keys.add(listener.casefold())
                characters.append(listener)
            if character_id is not None and character_id not in character_id_keys:
                character_id_keys.add(character_id)
                character_ids.append(character_id)

        previous_pending = _clean_names(state.get("pending_characters", []))
        key_validated = bool(state.get("key_validated"))
        verified = bool(state.get("identity_verified"))
        verified_names = set() if not verified else {
            item for item in previous_character_keys if item not in {
                pending.casefold() for pending in previous_pending
            }
        }
        pending_characters = [
            item for item in characters if item.casefold() not in verified_names
        ]
        previous_pending_ids = set(
            _clean_character_ids(state.get("pending_character_ids", []))
        )
        verified_ids = (
            set()
            if not verified
            else previous_character_ids - previous_pending_ids
        )
        pending_character_ids = [
            item for item in character_ids if item not in verified_ids
        ]

        state.update({
            "api_key": api_key,
            "key_fingerprint": _key_fingerprint(api_key),
            "initialized": True,
            "processed_files": _clean_file_names(processed_files)[
                -MAX_TRACKED_FILES:
            ],
            "characters": characters,
            "pending_characters": pending_characters,
            "listener_character_ids": character_ids,
            "pending_character_ids": pending_character_ids,
        })
        self.state_store.save(state)
        return IdentityScanResult(
            characters=characters,
            pending_characters=pending_characters,
            pending_files=sorted(self._pending_files),
            processed_count=processed_count,
            initial_scan=initial_scan,
            key_validated=key_validated,
            identity_verified=verified,
            character_ids=character_ids,
            pending_character_ids=pending_character_ids,
        )

    def _discover_entries(self) -> list[tuple[tuple[int, str], Path]]:
        """Return log entries ordered by modification time and file name."""
        entries: list[tuple[tuple[int, str], Path]] = []
        try:
            with os.scandir(self.log_dir) as iterator:
                for entry in iterator:
                    if not entry.name.casefold().endswith(".txt"):
                        continue
                    channel = normalize_channel_name(
                        channel_name_from_path(Path(entry.name))
                    )
                    if channel not in LOCAL_CHANNELS and not any(
                        channel.startswith(f"{item}_")
                        for item in LOCAL_CHANNELS
                    ):
                        continue
                    try:
                        if not entry.is_file():
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    marker = (int(stat.st_mtime_ns), entry.name.casefold())
                    entries.append((marker, Path(entry.path)))
        except OSError:
            return []
        cutoff_ns = time.time_ns() - RECENT_LOCAL_LOG_SECONDS * 1_000_000_000
        entries = [item for item in entries if item[0][0] >= cutoff_ns]
        latest_by_owner: dict[str, tuple[tuple[int, str], Path]] = {}
        for item in entries:
            owner_key = _log_owner_key(item[1])
            previous = latest_by_owner.get(owner_key)
            if previous is None or item[0] > previous[0]:
                latest_by_owner[owner_key] = item
        return sorted(latest_by_owner.values(), key=lambda item: item[0])

    def mark_key_validated(self) -> None:
        state = self.state_store.load()
        state["key_validated"] = True
        self.state_store.save(state)

    def mark_verified(self, names: list[str]) -> None:
        state = self.state_store.load()
        verified_keys = {str(item).strip().casefold() for item in names if str(item).strip()}
        pending = [
            item for item in _clean_names(state.get("pending_characters", []))
            if item.casefold() not in verified_keys
        ]
        state["pending_characters"] = pending
        state["key_validated"] = True
        state["identity_verified"] = not pending and bool(
            state.get("characters")
        )
        self.state_store.save(state)

    def mark_character_ids_verified(self, character_ids: list[int]) -> None:
        state = self.state_store.load()
        verified_ids = set(_clean_character_ids(character_ids))
        pending_ids = [
            item
            for item in _clean_character_ids(
                state.get("pending_character_ids", [])
            )
            if item not in verified_ids
        ]
        state["pending_character_ids"] = pending_ids
        state["key_validated"] = True
        state["identity_verified"] = not pending_ids and bool(
            state.get("listener_character_ids")
        )
        self.state_store.save(state)


def _listener_from_file(path: Path) -> str:
    try:
        data = path.read_bytes()[:65536]
    except OSError:
        return ""
    if not data:
        return ""
    encoding = detect_encoding(data)
    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    for line in text.splitlines():
        listener = parse_listener_line(line)
        if listener:
            return listener
    return ""


def _clean_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _clean_file_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = Path(str(value or "").strip()).name
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _file_signature(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def _directory_mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _log_owner_key(path: Path) -> str:
    """Return the trailing character ID used to group Local chatlogs."""
    character_id = _character_id_from_log_path(path)
    if character_id is not None:
        return f"character:{character_id}"
    return f"file:{path.name.casefold()}"


def _character_id_from_log_path(path: Path) -> int | None:
    match = TRAILING_CHARACTER_ID_RE.search(path.stem.strip())
    if not match:
        return None
    character_id = int(match.group("character_id"))
    return character_id if character_id > 0 else None


def _clean_character_ids(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            character_id = int(value)
        except (TypeError, ValueError):
            continue
        if character_id > 0 and character_id not in seen:
            seen.add(character_id)
            result.append(character_id)
    return result


def _clean_character_identities(values: Any) -> list[dict[str, Any]]:
    """Return valid, case-insensitively deduplicated character identities."""
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        character_name = str(value.get("character_name") or "").strip()
        try:
            character_id = int(value.get("character_id"))
        except (TypeError, ValueError):
            continue
        if not character_name or character_id <= 0:
            continue
        item = {
            "character_id": character_id,
            "character_name": character_name,
        }
        key = character_name.casefold()
        if key in indexes:
            result[indexes[key]] = item
        else:
            indexes[key] = len(result)
            result.append(item)
    return result


def _key_fingerprint(api_key: str) -> str:
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
