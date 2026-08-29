"""Thread-safe in-memory TTL cache used by the gateway."""

from __future__ import annotations

import time
from threading import RLock
from typing import Any


class TtlCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = max(1.0, float(ttl))
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = RLock()

    def get(self, key: str) -> tuple[bool, Any]:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item and item[0] > now:
                return True, item[1]
            if item:
                self._items.pop(key, None)
            return False, None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + self.ttl, value)

    def purge(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key, (expires_at, _value) in list(self._items.items()):
                if expires_at <= now:
                    self._items.pop(key, None)

    def size(self) -> int:
        self.purge()
        with self._lock:
            return len(self._items)
