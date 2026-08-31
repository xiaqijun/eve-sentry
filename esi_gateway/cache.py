"""Thread-safe in-memory TTL cache used by the gateway."""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import RLock
from typing import Any


class TtlCache:
    def __init__(self, ttl: float, max_entries: int = 4096, stale_grace: float = 300.0) -> None:
        self.ttl = max(1.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        self.stale_grace = max(0.0, float(stale_grace))
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._stale: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = RLock()
        self.evictions = 0

    def get(self, key: str) -> tuple[bool, Any]:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item and item[0] > now:
                self._items.move_to_end(key)
                return True, item[1]
            if item:
                self._items.pop(key, None)
                if now - item[0] <= self.stale_grace:
                    self._stale[key] = item
                    self._stale.move_to_end(key)
                self.evictions += 1
            return False, None

    def get_stale(self, key: str) -> tuple[bool, Any]:
        """Return an expired value while it remains inside the stale grace window."""
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key) or self._stale.get(key)
            if item is None:
                return False, None
            expires_at, value = item
            if expires_at <= now and now - expires_at <= self.stale_grace:
                if key in self._items:
                    self._items.move_to_end(key)
                else:
                    self._stale.move_to_end(key)
                return True, value
            self._stale.pop(key, None)
            return False, None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._stale.pop(key, None)
            self._items[key] = (time.monotonic() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
                self.evictions += 1

    def purge(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key, (expires_at, _value) in list(self._items.items()):
                if expires_at <= now:
                    self._items.pop(key, None)
                    if now - expires_at <= self.stale_grace:
                        self._stale[key] = (expires_at, _value)
                        self._stale.move_to_end(key)
                    self.evictions += 1
            for key, (expires_at, _value) in list(self._stale.items()):
                if now - expires_at > self.stale_grace:
                    self._stale.pop(key, None)
            while len(self._stale) > self.max_entries:
                self._stale.popitem(last=False)

    def size(self) -> int:
        self.purge()
        with self._lock:
            return len(self._items)
