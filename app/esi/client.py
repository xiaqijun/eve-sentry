"""HTTP client for public EVE Online ESI endpoints."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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

    def search_characters(
        self,
        character_id: int,
        access_token: str,
        search: str,
    ) -> list[int]:
        """Search character ids through the authenticated ESI search route."""
        query = urlencode(
            {
                "categories": "character",
                "search": str(search or "").strip(),
                "strict": "false",
            }
        )
        payload = self._request(
            "GET",
            f"/characters/{int(character_id)}/search/?{query}",
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            return []
        result: list[int] = []
        for value in payload.get("character", []) or []:
            try:
                entity_id = int(value)
            except (TypeError, ValueError):
                continue
            if entity_id > 0 and entity_id not in result:
                result.append(entity_id)
        return result

    def get_system(self, system_id: int) -> dict[str, Any]:
        """Fetch public solar-system information."""
        return self._request("GET", f"/universe/systems/{int(system_id)}/")

    def get_character_location(
        self,
        character_id: int,
        access_token: str,
    ) -> dict[str, Any]:
        """Fetch an authenticated character's current location."""
        return self._request(
            "GET",
            f"/characters/{int(character_id)}/location/",
            access_token=access_token,
        )

    def get_character_contacts(
        self,
        character_id: int,
        access_token: str,
    ) -> list[dict[str, Any]]:
        """Fetch authenticated character contacts and standings."""
        payload = self._request(
            "GET",
            f"/characters/{int(character_id)}/contacts/",
            access_token=access_token,
        )
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid contacts payload")
        return payload

    def get_character_standings(
        self,
        character_id: int,
        access_token: str,
    ) -> list[dict[str, Any]]:
        """Fetch the complete authenticated character standings snapshot."""
        payload = self._request(
            "GET",
            f"/characters/{int(character_id)}/standings/",
            access_token=access_token,
        )
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid standings payload")
        return payload

    def get_corporation_contacts(
        self,
        corporation_id: int,
        access_token: str,
    ) -> list[dict[str, Any]]:
        """Fetch authenticated corporation contacts and standings."""
        payload = self._request(
            "GET",
            f"/corporations/{int(corporation_id)}/contacts/",
            access_token=access_token,
        )
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid corporation contacts payload")
        return payload

    def get_alliance_contacts(
        self,
        alliance_id: int,
        access_token: str,
    ) -> list[dict[str, Any]]:
        """Fetch authenticated alliance contacts and standings."""
        payload = self._request(
            "GET",
            f"/alliances/{int(alliance_id)}/contacts/",
            access_token=access_token,
        )
        if not isinstance(payload, list):
            raise EsiApiError("ESI returned invalid alliance contacts payload")
        return payload

    def _request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        access_token: str | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
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
