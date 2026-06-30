"""Small JSON cache for ESI lookups."""

from __future__ import annotations

import json
from pathlib import Path
from time import time
from typing import Any


class EsiCache:
    """JSON-backed cache with per-entry expiry."""

    def __init__(self, path: str | Path = "esi_cache.json") -> None:
        self.path = Path(path)
        self._items: dict[str, dict[str, Any]] = self._load()

    def get(self, key: str) -> Any | None:
        """Return a cached value if it exists and has not expired."""
        item = self._items.get(key)
        if not item:
            return None
        expires_at = float(item.get("expires_at", 0))
        if expires_at <= time():
            self._items.pop(key, None)
            return None
        return item.get("value")

    def set(self, key: str, value: Any, ttl_seconds: int = 86400) -> None:
        """Store a value with an expiry."""
        self._items[key] = {
            "value": value,
            "expires_at": time() + max(1, int(ttl_seconds)),
        }

    def save(self) -> None:
        """Persist the cache to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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

