"""Reliable latest-state uploader for OCR snapshots and heartbeats."""

from __future__ import annotations

import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from app.intel_client import IntelApiClient, IntelApiError


@dataclass
class _PendingUpload:
    key: str
    payload: dict[str, Any]
    metadata: dict[str, Any]
    expires_at: float
    generation: int


class ReliableUploadManager(QObject):
    """Keep only current client state and retry it without blocking Qt."""

    state_changed = pyqtSignal(str, str)
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
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._clock = clock
        self._random = random_source
        self._condition = threading.Condition()
        self._snapshots: dict[str, _PendingUpload] = {}
        self._heartbeat: _PendingUpload | None = None
        self._generation: dict[str, int] = {}
        self._retry_index = 0
        self._retry_at = 0.0
        self._running = True
        self._state = "reconnecting"
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
                expires_at=self._clock() + max(0.1, float(ttl)),
                generation=generation,
            )
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
            self._heartbeat = _PendingUpload(
                key="heartbeat",
                payload=dict(payload),
                metadata=dict(metadata or {}),
                expires_at=self._clock() + max(1.0, float(ttl)),
                generation=generation,
            )
            self._condition.notify_all()

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel queued state and briefly wait for the active request."""
        with self._condition:
            self._running = False
            self._snapshots.clear()
            self._heartbeat = None
            self._condition.notify_all()
        self._thread.join(max(0.0, float(timeout)))
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def pending_snapshot_count(self) -> int:
        with self._condition:
            return len(self._snapshots)

    def _run(self) -> None:
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

    def _send(self, upload: _PendingUpload) -> None:
        try:
            if upload.key == "heartbeat":
                self._client.post_heartbeat(**upload.payload)
            else:
                self._client.post_ocr_snapshot(**upload.payload)
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
                cached = bool(self._snapshots)
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
        if upload.key == "heartbeat":
            self.heartbeat_uploaded.emit(metadata)
        else:
            self.snapshot_uploaded.emit(metadata)

    def _discard(self, upload: _PendingUpload) -> None:
        with self._condition:
            if upload.key == "heartbeat":
                if self._heartbeat is upload:
                    self._heartbeat = None
                return
            current = self._snapshots.get(upload.key)
            if current is upload:
                self._snapshots.pop(upload.key, None)

    def _next_upload(self) -> _PendingUpload | None:
        if self._snapshots:
            return min(self._snapshots.values(), key=lambda item: item.expires_at)
        return self._heartbeat

    def _drop_expired(self, now: float) -> None:
        self._snapshots = {
            key: upload
            for key, upload in self._snapshots.items()
            if upload.expires_at > now
        }
        if self._heartbeat is not None and self._heartbeat.expires_at <= now:
            self._heartbeat = None

    def _set_state(self, state: str, label: str) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(state, label)
