"""Small JSON cache for ESI lookups."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter, deque
from pathlib import Path
from threading import RLock
from time import monotonic, time
from typing import Any


class EsiCache:
    """JSON-backed cache with per-entry expiry."""

    def __init__(
        self,
        path: str | Path = "esi_cache.json",
        *,
        max_entries: int = 20000,
        stale_grace_seconds: int = 7 * 86400,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.stale_grace_seconds = max(0, int(stale_grace_seconds))
        self._lock = RLock()
        self._items: dict[str, dict[str, Any]] = self._load()
        self._evictions = 0
        self._lookups: Counter[str] = Counter()
        self._lookup_times: deque[float] = deque(maxlen=10000)
        with self._lock:
            self._prune_locked(time())

    def get(self, key: str) -> Any | None:
        """Return a cached value if it exists and has not expired."""
        namespace = self._namespace(key)
        with self._lock:
            item = self._items.get(key)
            hit = bool(item) and float(item.get("expires_at", 0)) > time()
            self._lookups["lookups"] += 1
            self._lookups[f"{namespace}:lookups"] += 1
            self._lookups["hits" if hit else "misses"] += 1
            self._lookups[f"{namespace}:{'hits' if hit else 'misses'}"] += 1
            self._lookup_times.append(monotonic())
            return item.get("value") if hit and item is not None else None

    def get_stale(self, key: str) -> Any | None:
        """Return a cached value even when its expiry has passed."""
        namespace = self._namespace(key)
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            self._lookups["stale_hits"] += 1
            self._lookups[f"{namespace}:stale_hits"] += 1
            return item.get("value")

    def metadata(self, key: str) -> dict[str, Any]:
        """Return cache metadata for an entry without exposing the value."""
        with self._lock:
            item = self._items.get(key)
            if not item:
                return {"cache_status": "miss"}
            expires_at = float(item.get("expires_at", 0))
            status = "cached" if expires_at > time() else "stale"
            metadata = {
                "cache_status": status,
                "expires_at": expires_at,
            }
            fetched_at = item.get("fetched_at")
            if fetched_at not in {None, ""}:
                metadata["fetched_at"] = fetched_at
            return metadata

    def set(self, key: str, value: Any, ttl_seconds: int = 86400) -> None:
        """Store a value with an expiry."""
        now = time()
        with self._lock:
            self._items[key] = {
                "value": value,
                "fetched_at": now,
                "expires_at": now + max(1, int(ttl_seconds)),
            }

    def invalidate(self, key: str) -> bool:
        """Remove one entry and report whether it existed."""
        with self._lock:
            return self._items.pop(key, None) is not None

    def invalidate_namespace(self, namespace: str) -> int:
        """Remove all entries in a namespace and return the count."""
        prefix = f"{str(namespace or '').strip().casefold()}:"
        if prefix == ":":
            return 0
        with self._lock:
            keys = [key for key in self._items if key.casefold().startswith(prefix)]
            for key in keys:
                self._items.pop(key, None)
            self._evictions += len(keys)
            return len(keys)

    def save(self) -> None:
        """Persist the cache to disk."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._prune_locked(time())
            payload = json.dumps(self._items, ensure_ascii=False, indent=2)
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    temporary.write(payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, self.path)
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)

    def snapshot(self) -> dict[str, Any]:
        """Return 114-side business cache metrics without cached values."""
        now = time()
        monotonic_now = monotonic()
        with self._lock:
            namespace_entries: dict[str, Counter[str]] = {}
            active_entries = 0
            stale_entries = 0
            for key, item in self._items.items():
                namespace = self._namespace(key)
                entries = namespace_entries.setdefault(namespace, Counter())
                entries["total"] += 1
                if float(item.get("expires_at", 0)) > now:
                    entries["active"] += 1
                    active_entries += 1
                else:
                    entries["stale"] += 1
                    stale_entries += 1

            recent_lookups = sum(
                timestamp >= monotonic_now - 60.0
                for timestamp in self._lookup_times
            )
            namespaces = sorted(
                set(namespace_entries)
                | {
                    key.split(":", 1)[0]
                    for key in self._lookups
                    if ":" in key
                }
            )
            namespace_metrics = {}
            for namespace in namespaces:
                lookups = self._lookups[f"{namespace}:lookups"]
                hits = self._lookups[f"{namespace}:hits"]
                misses = self._lookups[f"{namespace}:misses"]
                entries = namespace_entries.get(namespace, Counter())
                namespace_metrics[namespace] = {
                    "lookups": lookups,
                    "hits": hits,
                    "misses": misses,
                    "stale_hits": self._lookups[f"{namespace}:stale_hits"],
                    "hit_rate": round(hits / max(1, lookups), 4),
                    "entries": entries["total"],
                    "active_entries": entries["active"],
                    "stale_entries": entries["stale"],
                    "evictions": self._evictions,
                }

            lookups = self._lookups["lookups"]
            hits = self._lookups["hits"]
            return {
                "totals": {
                    "lookups": lookups,
                    "hits": hits,
                    "misses": self._lookups["misses"],
                    "stale_hits": self._lookups["stale_hits"],
                    "evictions": self._evictions,
                    "hit_rate": round(hits / max(1, lookups), 4),
                    "lookup_rate_per_second": round(recent_lookups / 60.0, 4),
                },
                "entries": {
                    "total": len(self._items),
                    "active": active_entries,
                    "stale": stale_entries,
                },
                "namespaces": namespace_metrics,
            }

    @staticmethod
    def _namespace(key: str) -> str:
        namespace = str(key or "").split(":", 1)[0].strip().casefold()
        return namespace or "other"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                result[str(key)] = value
        return result

    def _prune_locked(self, now: float) -> int:
        removed = 0
        for key, item in list(self._items.items()):
            expires_at = float(item.get("expires_at", 0))
            fetched_at = item.get("fetched_at")
            if (
                expires_at <= now
                and fetched_at not in {None, ""}
                and now - float(fetched_at) > self.stale_grace_seconds
            ):
                self._items.pop(key, None)
                removed += 1

        if len(self._items) > self.max_entries:
            overflow = len(self._items) - self.max_entries
            oldest = sorted(
                self._items.items(),
                key=lambda pair: float(pair[1].get("fetched_at", 0)),
            )[:overflow]
            for key, _item in oldest:
                self._items.pop(key, None)
            removed += len(oldest)
        self._evictions += removed
        return removed
