"""HTTP client for public EVE Online ESI endpoints."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EsiApiError(RuntimeError):
    """Raised when ESI returns an error or invalid response."""


class EsiClient:
    """Minimal JSON client for public ESI data."""

    def __init__(
        self,
        base_url: str = "https://esi.evetech.net/latest",
        timeout: float = 10.0,
        user_agent: str = "eve-sentry/0.1",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent

    def resolve_ids(self, names: list[str]) -> dict[str, Any]:
        """Resolve names to ESI ids via POST /universe/ids/."""
        return self._request("POST", "/universe/ids/", payload=names)

    def resolve_names(self, ids: list[int]) -> list[dict[str, Any]]:
        """Resolve ids to names via POST /universe/names/."""
        payload = self._request("POST", "/universe/names/", payload=ids)
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid names payload")
        return payload

    def get_character(self, character_id: int) -> dict[str, Any]:
        """Fetch public character information."""
        return self._request("GET", f"/characters/{int(character_id)}/")

    def get_corporation(self, corporation_id: int) -> dict[str, Any]:
        """Fetch public corporation information."""
        return self._request("GET", f"/corporations/{int(corporation_id)}/")

    def get_alliance(self, alliance_id: int) -> dict[str, Any]:
        """Fetch public alliance information."""
        return self._request("GET", f"/alliances/{int(alliance_id)}/")

    def get_system(self, system_id: int) -> dict[str, Any]:
        """Fetch public solar-system information."""
        return self._request("GET", f"/universe/systems/{int(system_id)}/")

    def _request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise EsiApiError(self._read_error_message(exc)) from exc
        except URLError as exc:
            raise EsiApiError(str(exc.reason)) from exc
        except OSError as exc:
            raise EsiApiError(str(exc)) from exc

        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise EsiApiError("ESI returned invalid JSON") from exc

    def _read_error_message(self, exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            if isinstance(payload, dict):
                if payload.get("error"):
                    return str(payload["error"])
                if payload.get("message"):
                    return str(payload["message"])
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        return f"ESI HTTP {exc.code}"

