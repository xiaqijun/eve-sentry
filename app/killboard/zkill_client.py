"""zKillboard API client with conservative local caching."""

from __future__ import annotations

import gzip
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.esi.cache import EsiCache


class ZKillboardApiError(RuntimeError):
    """Raised when zKillboard cannot satisfy a request."""


class ZKillboardClient:
    """Small JSON client for recent zKillboard activity."""

    def __init__(
        self,
        base_url: str = "https://zkillboard.com/api",
        timeout: float = 10.0,
        user_agent: str = "eve-sentry/0.1",
        cache: EsiCache | None = None,
        ttl_seconds: int = 600,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.cache = cache
        self.ttl_seconds = ttl_seconds

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

    def _get_list(self, path: str) -> list[dict[str, Any]]:
        cache_key = f"zkill:{path}"
        stale: list[dict[str, Any]] | None = None
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if isinstance(cached, list):
                return cached
            stale_cached = self.cache.get_stale(cache_key)
            if isinstance(stale_cached, list):
                stale = stale_cached

        try:
            payload = self._request(path)
            if not isinstance(payload, list):
                raise ZKillboardApiError("zKillboard returned a non-list payload")
        except ZKillboardApiError:
            if stale is not None:
                return stale
            raise
        if self.cache is not None:
            self.cache.set(cache_key, payload, ttl_seconds=self.ttl_seconds)
            self.cache.save()
        return payload

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
            raise ZKillboardApiError(f"zKillboard HTTP {exc.code}") from exc
        except URLError as exc:
            raise ZKillboardApiError(str(exc.reason)) from exc
        except OSError as exc:
            raise ZKillboardApiError(str(exc)) from exc

        if not body:
            return []
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ZKillboardApiError("zKillboard returned invalid JSON") from exc
