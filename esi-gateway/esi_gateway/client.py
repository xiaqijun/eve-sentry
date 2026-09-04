"""Minimal HTTP client for public EVE Online ESI endpoints.

Authenticated ESI methods deliberately do not exist in this package.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EsiApiError(RuntimeError):
    """Raised when ESI returns an error or invalid response."""


class EsiClient:
    """Small JSON client exposing only public ESI operations."""

    def __init__(
        self,
        base_url: str = "https://esi.evetech.net/latest",
        timeout: float = 10.0,
        user_agent: str = "eve-sentry-esi-gateway/1.0",
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.user_agent = str(user_agent)
        self._opener = opener

    def resolve_ids(self, names: list[str]) -> dict[str, Any]:
        return self._request("POST", "/universe/ids/", payload=names)

    def resolve_names(self, ids: list[int]) -> list[dict[str, Any]]:
        payload = self._request("POST", "/universe/names/", payload=ids)
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid names payload")
        return payload

    def get_character(self, character_id: int) -> dict[str, Any]:
        return self._request("GET", f"/characters/{int(character_id)}/")

    def get_character_affiliations(self, character_ids: list[int]) -> list[dict[str, Any]]:
        ids = [int(character_id) for character_id in character_ids]
        if not ids or len(ids) > 1000 or any(character_id <= 0 for character_id in ids):
            raise ValueError("character_ids must contain 1-1000 positive IDs")
        payload = self._request("POST", "/characters/affiliation/", payload=ids)
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid affiliation payload")
        return [item for item in payload if isinstance(item, dict)]

    def get_corporation(self, corporation_id: int) -> dict[str, Any]:
        return self._request("GET", f"/corporations/{int(corporation_id)}/")

    def get_alliance(self, alliance_id: int) -> dict[str, Any]:
        return self._request("GET", f"/alliances/{int(alliance_id)}/")

    def get_system(self, system_id: int) -> dict[str, Any]:
        return self._request("GET", f"/universe/systems/{int(system_id)}/")

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise EsiApiError(self._read_error_message(exc)) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise EsiApiError(str(getattr(exc, "reason", exc))) from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise EsiApiError("ESI returned invalid JSON") from exc

    @staticmethod
    def _read_error_message(exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(payload, dict):
                if payload.get("error"):
                    return str(payload["error"])
                if payload.get("message"):
                    return str(payload["message"])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        return f"ESI HTTP {exc.code}"
