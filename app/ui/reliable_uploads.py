"""Reliable latest-state uploader for OCR snapshots and heartbeats."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from app.intel_client import IntelApiClient, IntelApiError

logger = logging.getLogger(__name__)


def default_snapshot_state_path() -> Path:
    """Return the per-user path for the latest offline detector snapshots."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "EVE Sentry" if base else Path.home() / ".eve-sentry"
    return root / "detector_offline_snapshots.json"


@dataclass
class _PendingUpload:
    key: str
    payload: dict[str, Any]
    metadata: dict[str, Any]
    expires_at: float
    generation: int
    expires_wall_at: float


class ReliableUploadManager(QObject):
    """Keep only current client state and retry it without blocking Qt."""

    state_changed = pyqtSignal(str, str)
    presence_uploaded = pyqtSignal(object)
    snapshot_uploaded = pyqtSignal(object)
    heartbeat_uploaded = pyqtSignal(object)

    _BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

    def __init__(
        self,
        client: IntelApiClient,
        parent: QObject | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        state_path: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._clock = clock
        self._random = random_source
        self._state_path = Path(state_path) if state_path is not None else default_snapshot_state_path()
        self._condition = threading.Condition()
        self._presence: dict[str, _PendingUpload] = {}
        self._snapshots: dict[str, _PendingUpload] = {}
        self._heartbeat: _PendingUpload | None = None
        self._generation: dict[str, int] = {}
        self._retry_index = 0
        self._retry_at = 0.0
        self._running = True
        self._load_snapshots()
        self._state = (
            "offline_cached"
            if self._presence or self._snapshots
            else "reconnecting"
        )
        self._thread = threading.Thread(
            target=self._run,
            name="eve-sentry-upload",
            daemon=True,
        )
        self._thread.start()

    @property
    def state(self) -> str:
        return self._state

    def submit_snapshot(
        self,
        key: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        ttl: float = 15.0,
    ) -> int:
        """Replace the pending snapshot for one window, including empty lists."""
        normalized_key = str(key or payload.get("client_id") or "window")
        with self._condition:
            generation = self._generation.get(normalized_key, 0) + 1
            self._generation[normalized_key] = generation
            captured_at = datetime.now(timezone.utc).isoformat()
            ttl_seconds = max(0.1, float(ttl))
            enriched = dict(payload)
            enriched.update(
                {
                    "snapshot_id": str(uuid.uuid4()),
                    "sequence": generation,
                    "captured_at": captured_at,
                    "seen_at": captured_at,
                }
            )
            self._snapshots[normalized_key] = _PendingUpload(
                key=normalized_key,
                payload=enriched,
                metadata=dict(metadata or {}),
                expires_at=self._clock() + ttl_seconds,
                generation=generation,
                expires_wall_at=time.time() + ttl_seconds,
            )
            self._persist_snapshots_locked()
            self._condition.notify_all()
        return generation

    def submit_presence(
        self,
        key: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        ttl: float = 60.0,
    ) -> int:
        """Replace the latest visual hostile count for one monitored window."""
        normalized_key = str(key or payload.get("client_id") or "window")
        generation_key = f"presence:{normalized_key}"
        with self._condition:
            generation = self._generation.get(generation_key, 0) + 1
            self._generation[generation_key] = generation
            captured_at = datetime.now(timezone.utc).isoformat()
            ttl_seconds = max(1.0, float(ttl))
            enriched = dict(payload)
            enriched.update(
                {
                    "snapshot_id": str(uuid.uuid4()),
                    "sequence": generation,
                    "captured_at": captured_at,
                    "seen_at": captured_at,
                }
            )
            self._presence[normalized_key] = _PendingUpload(
                key=generation_key,
                payload=enriched,
                metadata=dict(metadata or {}),
                expires_at=self._clock() + ttl_seconds,
                generation=generation,
                expires_wall_at=time.time() + ttl_seconds,
            )
            self._persist_snapshots_locked()
            self._condition.notify_all()
        return generation

    def submit_heartbeat(
        self,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        ttl: float = 60.0,
    ) -> None:
        """Coalesce heartbeats so recovery never replays obsolete runtime state."""
        with self._condition:
            generation = (self._heartbeat.generation + 1) if self._heartbeat else 1
            ttl_seconds = max(1.0, float(ttl))
            self._heartbeat = _PendingUpload(
                key="heartbeat",
                payload=dict(payload),
                metadata=dict(metadata or {}),
                expires_at=self._clock() + ttl_seconds,
                generation=generation,
                expires_wall_at=time.time() + ttl_seconds,
            )
            self._condition.notify_all()

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel queued state and briefly wait for the active request."""
        with self._condition:
            self._running = False
            self._persist_snapshots_locked()
            self._heartbeat = None
            self._condition.notify_all()
        wait_seconds = max(0.0, float(timeout))
        if wait_seconds:
            self._thread.join(wait_seconds)

    def pending_snapshot_count(self) -> int:
        with self._condition:
            return len(self._snapshots)

    def pending_presence_count(self) -> int:
        with self._condition:
            return len(self._presence)

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    if not self._running:
                        return
                    now = self._clock()
                    self._drop_expired(now)
                    if now < self._retry_at:
                        self._condition.wait(min(0.5, self._retry_at - now))
                        continue
                    upload = self._next_upload()
                    if upload is None:
                        self._condition.wait(0.5)
                        continue
                self._send(upload)
        finally:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()

    def _send(self, upload: _PendingUpload) -> None:
        response: dict[str, Any] | None = None
        try:
            if upload.key == "heartbeat":
                response = self._client.post_heartbeat(**upload.payload)
            elif upload.key.startswith("presence:"):
                response = self._client.post_hostile_presence(**upload.payload)
            else:
                response = self._client.post_ocr_snapshot(**upload.payload)
        except IntelApiError as exc:
            if getattr(exc, "status_code", None) in {401, 403}:
                self._set_state("authentication_failed", "认证失效")
                with self._condition:
                    self._retry_at = float("inf")
                return
            if not getattr(exc, "transient", True):
                self._discard(upload)
                return
            with self._condition:
                base = self._BACKOFF[min(self._retry_index, len(self._BACKOFF) - 1)]
                self._retry_index += 1
                jitter = 0.8 + self._random() * 0.4
                self._retry_at = self._clock() + base * jitter
                cached = bool(self._presence or self._snapshots)
            self._set_state(
                "offline_cached" if cached else "reconnecting",
                "离线缓存" if cached else "重连中",
            )
            return

        self._retry_index = 0
        self._retry_at = 0.0
        self._discard(upload)
        self._set_state("online", "在线")
        metadata = dict(upload.metadata)
        metadata["generation"] = upload.generation
        if response is not None:
            metadata["response"] = response
        if upload.key == "heartbeat":
            self.heartbeat_uploaded.emit(metadata)
        elif upload.key.startswith("presence:"):
            self.presence_uploaded.emit(metadata)
        else:
            self.snapshot_uploaded.emit(metadata)

    def _discard(self, upload: _PendingUpload) -> None:
        with self._condition:
            if upload.key == "heartbeat":
                if self._heartbeat is upload:
                    self._heartbeat = None
                return
            if upload.key.startswith("presence:"):
                normalized_key = upload.key.removeprefix("presence:")
                current = self._presence.get(normalized_key)
                if current is upload:
                    self._presence.pop(normalized_key, None)
                    self._persist_snapshots_locked()
                return
            current = self._snapshots.get(upload.key)
            if current is upload:
                self._snapshots.pop(upload.key, None)
                self._persist_snapshots_locked()

    def _next_upload(self) -> _PendingUpload | None:
        if self._presence:
            return min(self._presence.values(), key=lambda item: item.expires_at)
        if self._heartbeat is not None:
            return self._heartbeat
        if self._snapshots:
            return min(self._snapshots.values(), key=lambda item: item.expires_at)
        return None

    def _drop_expired(self, now: float) -> None:
        previous_presence_count = len(self._presence)
        self._presence = {
            key: upload
            for key, upload in self._presence.items()
            if upload.expires_at > now
        }
        previous_count = len(self._snapshots)
        self._snapshots = {
            key: upload
            for key, upload in self._snapshots.items()
            if upload.expires_at > now
        }
        if (
            len(self._presence) != previous_presence_count
            or len(self._snapshots) != previous_count
        ):
            self._persist_snapshots_locked()
        if self._heartbeat is not None and self._heartbeat.expires_at <= now:
            self._heartbeat = None

    def _load_snapshots(self) -> None:
        """Restore valid, unexpired snapshots without restoring request metadata."""
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        records = raw.get("snapshots") if isinstance(raw, dict) else None
        presence_records = raw.get("presence") if isinstance(raw, dict) else None
        has_record_maps = isinstance(records, dict) or isinstance(presence_records, dict)
        records = records if isinstance(records, dict) else {}
        presence_records = presence_records if isinstance(presence_records, dict) else {}
        if not has_record_maps:
            return
        now_wall = time.time()
        now_mono = self._clock()
        for key, record in records.items():
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            expires_wall_at = _as_positive_float(record.get("expires_at"))
            if not isinstance(payload, dict) or expires_wall_at <= now_wall:
                continue
            remaining = expires_wall_at - now_wall
            generation = _as_positive_int(record.get("generation")) or 1
            normalized_key = str(key or payload.get("client_id") or "window")
            self._snapshots[normalized_key] = _PendingUpload(
                key=normalized_key,
                payload=_redact_sensitive(payload),
                metadata={},
                expires_at=now_mono + remaining,
                generation=generation,
                expires_wall_at=expires_wall_at,
            )
            self._generation[normalized_key] = generation
        for key, record in presence_records.items():
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            expires_wall_at = _as_positive_float(record.get("expires_at"))
            if not isinstance(payload, dict) or expires_wall_at <= now_wall:
                continue
            remaining = expires_wall_at - now_wall
            generation = _as_positive_int(record.get("generation")) or 1
            normalized_key = str(key or payload.get("client_id") or "window")
            generation_key = f"presence:{normalized_key}"
            self._presence[normalized_key] = _PendingUpload(
                key=generation_key,
                payload=_redact_sensitive(payload),
                metadata={},
                expires_at=now_mono + remaining,
                generation=generation,
                expires_wall_at=expires_wall_at,
            )
            self._generation[generation_key] = generation
        if not self._presence and not self._snapshots:
            try:
                self._state_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _persist_snapshots_locked(self) -> None:
        """Atomically persist only current snapshots and their wall-clock TTL."""
        valid_presence = {
            key: {
                "payload": _redact_sensitive(upload.payload),
                "expires_at": upload.expires_wall_at,
                "generation": upload.generation,
            }
            for key, upload in self._presence.items()
            if upload.expires_wall_at > time.time()
        }
        valid = {
            key: {
                "payload": _redact_sensitive(upload.payload),
                "expires_at": upload.expires_wall_at,
                "generation": upload.generation,
            }
            for key, upload in self._snapshots.items()
            if upload.expires_wall_at > time.time()
        }
        if not valid_presence and not valid:
            try:
                self._state_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        temporary = self._state_path.with_name(self._state_path.name + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "presence": valid_presence,
                        "snapshots": valid,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)
        except OSError:
            logger.warning("Unable to persist offline OCR snapshots", exc_info=True)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _set_state(self, state: str, label: str) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(state, label)


def _as_positive_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0 else 0.0


def _as_positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _redact_sensitive(value: Any) -> Any:
    """Remove credential-like fields before data is written to disk."""
    sensitive = {"api_key", "authorization", "password", "secret", "token"}
    if isinstance(value, dict):
        return {
            str(key): _redact_sensitive(item)
            for key, item in value.items()
            if str(key).casefold() not in sensitive
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value
