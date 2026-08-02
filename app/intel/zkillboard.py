"""Rate-limited zKillboard character statistics lookup."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://zkillboard.com/api"
DEFAULT_USER_AGENT = "eve-sentry/1.0 (+https://github.com/xiaqijun/eve-sentry)"


@dataclass(frozen=True)
class _CacheEntry:
    value: dict[str, Any] | None
    expires_at: float


class ZkillboardClient:
    """Fetch normalized character stats while respecting zKillboard limits."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 4.0,
        cache_ttl_seconds: float = 12 * 60 * 60,
        failure_ttl_seconds: float = 10 * 60,
        min_request_interval_seconds: float = 1.1,
        max_cache_entries: int = 4096,
        opener: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT).strip()
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self.failure_ttl_seconds = max(0.0, float(failure_ttl_seconds))
        self.min_request_interval_seconds = max(
            0.0, float(min_request_interval_seconds)
        )
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._opener = opener or urlopen
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._cache: dict[int, _CacheEntry] = {}
        self._cache_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._last_request_at: float | None = None

    def character_stats(self, character_id: int) -> dict[str, Any] | None:
        """Return cached normalized zKillboard stats for one character."""
        character_id = _positive_int(character_id)
        if character_id is None:
            return None

        now = self._monotonic()
        with self._cache_lock:
            cached = self._cache.get(character_id)
            if cached is not None and now < cached.expires_at:
                return dict(cached.value) if cached.value is not None else None

        value = self._request_character_stats(character_id)
        ttl = self.cache_ttl_seconds if value is not None else self.failure_ttl_seconds
        with self._cache_lock:
            self._cache[character_id] = _CacheEntry(value, self._monotonic() + ttl)
            self._trim_cache()
        return dict(value) if value is not None else None

    def _request_character_stats(self, character_id: int) -> dict[str, Any] | None:
        with self._request_lock:
            now = self._monotonic()
            if self._last_request_at is not None:
                remaining = (
                    self.min_request_interval_seconds - (now - self._last_request_at)
                )
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_at = self._monotonic()

            url = f"{self.base_url}/stats/characterID/{character_id}/"
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                },
            )
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
                return None

        return _normalize_stats(payload, character_id, url)

    def _trim_cache(self) -> None:
        while len(self._cache) > self.max_cache_entries:
            oldest_id = min(
                self._cache,
                key=lambda item: self._cache[item].expires_at,
            )
            self._cache.pop(oldest_id, None)


def _normalize_stats(
    payload: Any,
    character_id: int,
    source_url: str,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    result: dict[str, Any] = {
        "source": "zkillboard",
        "character_id": character_id,
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    fields = {
        "danger_ratio": ("dangerRatio", _bounded_ratio),
        "gang_ratio": ("gangRatio", _bounded_ratio),
        "solo_ratio": ("soloRatio", _bounded_ratio),
        "ships_destroyed": ("shipsDestroyed", _non_negative_int),
        "ships_lost": ("shipsLost", _non_negative_int),
        "isk_destroyed": ("iskDestroyed", _non_negative_number),
        "isk_lost": ("iskLost", _non_negative_number),
    }
    for output_key, (input_key, cleaner) in fields.items():
        value = cleaner(payload.get(input_key))
        if value is not None:
            result[output_key] = value

    if len(result) == 4:
        return None
    return result


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _non_negative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _non_negative_number(value: Any) -> int | float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number) if number.is_integer() else number


def _bounded_ratio(value: Any) -> int | float | None:
    number = _non_negative_number(value)
    if number is None or number > 100:
        return None
    return number
