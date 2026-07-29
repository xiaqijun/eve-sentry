"""Persistent HTTP transport for the desktop detector client."""

from __future__ import annotations

from typing import Any

import httpx

from app.intel_client import IntelApiClient, IntelApiError


class PersistentIntelApiClient(IntelApiClient):
    """Use one keep-alive connection pool for JSON API calls."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=60.0,
            ),
            headers={"User-Agent": "EVE-Sentry-Detector/1.0"},
        )

    def close(self) -> None:
        """Release pooled connections."""
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http.request(
                method,
                path,
                params=params,
                json=payload,
                headers=self._authorization_headers(),
            )
        except httpx.TransportError as exc:
            raise _api_error(str(exc), transient=True) from exc

        if response.is_error:
            message = f"HTTP {response.status_code}"
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("error"):
                    message = str(body["error"])
            except ValueError:
                pass
            raise _api_error(
                message,
                status_code=response.status_code,
                transient=response.status_code >= 500 or response.status_code == 429,
            )
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise _api_error("server returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise _api_error("server returned a non-object JSON payload")
        return data


def _api_error(
    message: str,
    *,
    status_code: int | None = None,
    transient: bool = False,
) -> IntelApiError:
    error = IntelApiError(message)
    error.status_code = status_code
    error.transient = bool(transient)
    return error
