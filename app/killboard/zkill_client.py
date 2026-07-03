"""zKillboard API client with conservative local caching."""

from __future__ import annotations

import gzip
import json
from time import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.esi.cache import EsiCache


class ZKillboardApiError(RuntimeError):
    """Raised when zKillboard cannot satisfy a request."""

    def __init__(
        self,
        message: str,
        failure_type: str = "api_error",
        http_status: int | None = None,
        retry_after_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


class ZKillboardClient:
    """Small JSON client for recent zKillboard activity."""

    def __init__(
        self,
        base_url: str = "https://zkillboard.com/api",
        timeout: float = 10.0,
        user_agent: str = "eve-sentry/0.1",
        cache: EsiCache | None = None,
        ttl_seconds: int = 600,
        backoff_seconds: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.cache = cache
        self.ttl_seconds = ttl_seconds
        self.backoff_seconds = backoff_seconds
        self._last_status: dict[str, dict[str, Any]] = {}

    def character_recent(self, character_id: int) -> list[dict[str, Any]]:
        """Return recent killmails involving a character."""
        return self._get_list(f"/characterID/{int(character_id)}/")

    def corporation_recent(self, corporation_id: int) -> list[dict[str, Any]]:
        """Return recent killmails involving a corporation."""
        return self._get_list(f"/corporationID/{int(corporation_id)}/")

    def alliance_recent(self, alliance_id: int) -> list[dict[str, Any]]:
        """Return recent killmails involving an alliance."""
        return self._get_list(f"/allianceID/{int(alliance_id)}/")

    def system_recent(self, system_id: int) -> list[dict[str, Any]]:
        """Return recent killmails in a solar system."""
        return self._get_list(f"/solarSystemID/{int(system_id)}/")

    def activity_status(self, scope: str, entity_id: int) -> dict[str, Any]:
        """Return the latest cache/request status for an activity lookup."""
        path = self._activity_path(scope, entity_id)
        if not path:
            return {}
        status = self._last_status.get(path)
        if status is not None:
            return dict(status)
        if self.cache is None:
            return {}
        metadata = self.cache.metadata(f"zkill:{path}")
        return metadata if metadata.get("cache_status") != "miss" else {}

    def _get_list(self, path: str) -> list[dict[str, Any]]:
        cache_key = f"zkill:{path}"
        stale: list[dict[str, Any]] | None = None
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if isinstance(cached, list):
                self._last_status[path] = self.cache.metadata(cache_key)
                return cached
            stale_cached = self.cache.get_stale(cache_key)
            if isinstance(stale_cached, list):
                stale = stale_cached

        if self._is_backing_off(path):
            self._last_status[path] = self._backoff_status(path, cache_key, stale)
            if stale is not None:
                return stale
            raise ZKillboardApiError(
                "zKillboard request skipped during backoff",
                failure_type="backoff",
            )

        try:
            payload = self._request(path)
            if not isinstance(payload, list):
                raise ZKillboardApiError(
                    "zKillboard returned a non-list payload",
                    failure_type="invalid_payload",
                )
        except ZKillboardApiError as exc:
            self._last_status[path] = self._failure_status(cache_key, exc, stale)
            if stale is not None:
                return stale
            raise
        if self.cache is not None:
            self.cache.set(cache_key, payload, ttl_seconds=self.ttl_seconds)
            self.cache.save()
            self._last_status[path] = self.cache.metadata(cache_key)
            self._last_status[path]["cache_status"] = "refreshed"
        else:
            self._last_status[path] = {"cache_status": "refreshed"}
        return payload

    def _is_backing_off(self, path: str) -> bool:
        status = self._last_status.get(path) or {}
        retry_after = _optional_float(status.get("retry_after"))
        return retry_after is not None and retry_after > time()

    def _backoff_status(
        self,
        path: str,
        cache_key: str,
        stale: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        previous = dict(self._last_status.get(path) or {})
        status = self._cache_status(cache_key, stale)
        status.update(
            {
                "request_status": "backoff",
                "error": previous.get(
                    "error",
                    "zKillboard request skipped during backoff",
                ),
            }
        )
        if "retry_after" in previous:
            status["retry_after"] = previous["retry_after"]
        return status

    def _failure_status(
        self,
        cache_key: str,
        exc: ZKillboardApiError,
        stale: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        status = self._cache_status(cache_key, stale)
        status.update(
            {
                "request_status": exc.failure_type,
                "error": str(exc),
            }
        )
        if exc.http_status is not None:
            status["http_status"] = exc.http_status
        retry_after_seconds = self._retry_after_seconds(exc)
        if retry_after_seconds > 0:
            status["retry_after"] = time() + retry_after_seconds
        return status

    def _cache_status(
        self,
        cache_key: str,
        stale: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if self.cache is None:
            return {"cache_status": "stale" if stale is not None else "miss"}
        metadata = self.cache.metadata(cache_key)
        return metadata if metadata.get("cache_status") != "miss" else {
            "cache_status": "stale" if stale is not None else "miss"
        }

    def _retry_after_seconds(self, exc: ZKillboardApiError) -> float:
        if exc.retry_after_seconds > 0:
            return exc.retry_after_seconds
        if exc.failure_type in {"rate_limited", "server_error", "network_error"}:
            return float(max(0, self.backoff_seconds))
        return 0.0

    def _activity_path(self, scope: str, entity_id: int) -> str:
        normalized = scope.strip().casefold()
        if normalized == "character":
            return f"/characterID/{int(entity_id)}/"
        if normalized == "corporation":
            return f"/corporationID/{int(entity_id)}/"
        if normalized == "alliance":
            return f"/allianceID/{int(entity_id)}/"
        if normalized == "system":
            return f"/solarSystemID/{int(entity_id)}/"
        return ""

    def _request(self, path: str) -> Any:
        request = Request(
            f"{self.base_url}{path}",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
        except HTTPError as exc:
            failure_type = "rate_limited" if exc.code in {420, 429} else (
                "server_error" if 500 <= exc.code <= 599 else "http_error"
            )
            retry_after = _optional_float(exc.headers.get("Retry-After"))
            raise ZKillboardApiError(
                f"zKillboard HTTP {exc.code}",
                failure_type=failure_type,
                http_status=exc.code,
                retry_after_seconds=retry_after or 0.0,
            ) from exc
        except URLError as exc:
            raise ZKillboardApiError(
                str(exc.reason),
                failure_type="network_error",
            ) from exc
        except OSError as exc:
            raise ZKillboardApiError(str(exc), failure_type="network_error") from exc

        if not body:
            return []
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ZKillboardApiError(
                "zKillboard returned invalid JSON",
                failure_type="invalid_payload",
            ) from exc


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
