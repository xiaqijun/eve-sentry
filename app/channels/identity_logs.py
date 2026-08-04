"""Incremental EVE Listener discovery with protected local state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.channels.local_system import parse_listener_line
from app.channels.log_watcher import detect_encoding
from app.esi.sso import default_token_protector, token_protector_from_name
from app.intel_client import INVALID_API_KEY_MESSAGE, is_valid_api_key


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
        state["processed_files"] = sorted({str(item) for item in state.get("processed_files", []) if str(item)})
        state["characters"] = _clean_names(state.get("characters", []))
        state["pending_characters"] = _clean_names(state.get("pending_characters", []))
        state["character_identities"] = _clean_character_identities(
            state.get("character_identities", [])
        )
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
        state = self.empty_state()
        state["api_key"] = api_key
        state["key_fingerprint"] = _key_fingerprint(api_key)
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
    """Read every historical log once, then only newly created log files."""

    def __init__(self, log_dir: str | Path, state_store: ClientAuthStateStore):
        self.log_dir = Path(log_dir)
        self.state_store = state_store

    def scan(self, api_key: str) -> IdentityScanResult:
        state = self.state_store.load()
        api_key = str(api_key or "").strip()
        if state.get("key_fingerprint") != _key_fingerprint(api_key):
            self.state_store.set_api_key(api_key)
            state = self.state_store.load()

        initial_scan = not bool(state.get("initialized"))
        processed = {str(item) for item in state.get("processed_files", [])}
        characters = _clean_names(state.get("characters", []))
        previous_character_keys = {item.casefold() for item in characters}
        character_keys = {item.casefold() for item in characters}
        processed_count = 0

        paths = []
        if self.log_dir.exists():
            paths = sorted(
                (path for path in self.log_dir.glob("*.txt") if path.is_file()),
                key=lambda path: path.name.casefold(),
            )
        candidates = [path for path in paths if path.name not in processed]
        pending_files: list[str] = []
        for path in candidates:
            listener = _listener_from_file(path)
            if not listener:
                pending_files.append(path.name)
                continue
            processed.add(path.name)
            processed_count += 1
            if listener.casefold() not in character_keys:
                character_keys.add(listener.casefold())
                characters.append(listener)

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

        state.update({
            "api_key": api_key,
            "key_fingerprint": _key_fingerprint(api_key),
            "initialized": True,
            "processed_files": sorted(processed),
            "characters": characters,
            "pending_characters": pending_characters,
        })
        self.state_store.save(state)
        return IdentityScanResult(
            characters=characters,
            pending_characters=pending_characters,
            pending_files=pending_files,
            processed_count=processed_count,
            initial_scan=initial_scan,
            key_validated=key_validated,
            identity_verified=verified,
        )

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
        state["identity_verified"] = not pending and bool(state.get("characters"))
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
