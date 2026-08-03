"""Persistent HTTP transport for the desktop detector client."""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.intel_client import IntelApiClient, IntelApiError


class PersistentIntelApiClient(IntelApiClient):
    """Use one keep-alive connection pool for JSON calls and SSE streams."""

    def __init__(self, *args, **kwargs) -> None:
        proxy = kwargs.pop("proxy", None)
        super().__init__(*args, **kwargs)
        self.proxy = str(
            proxy or os.environ.get("EVE_SENTRY_HTTP_PROXY") or ""
        ).strip() or None
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=60.0,
            ),
            headers={"User-Agent": "EVE-Sentry-Detector/1.0"},
            proxy=self.proxy,
            trust_env=self.proxy is None,
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
            raise _response_error(response)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise _api_error("server returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise _api_error("server returned a non-object JSON payload")
        return data

    def iter_events(
        self,
        since: str = "",
        last_event_id: str = "",
        limit: int = 50,
        timeout: float = 30.0,
        heartbeat: float | None = None,
        should_stop=None,
        include_bootstrap: bool = False,
        min_score: int | None = None,
        min_level: str = "",
    ):
        """Yield SSE events over the same pooled HTTP transport as JSON calls."""
        params = {"limit": str(limit), "timeout": str(timeout)}
        if heartbeat is not None:
            params["heartbeat"] = str(max(0.0, float(heartbeat)))
        if include_bootstrap:
            params["bootstrap"] = "true"
        if since:
            params["since"] = since
        if min_score is not None:
            params["min_score"] = str(min_score)
        if min_level:
            params["min_level"] = min_level
        headers = {
            "Accept": "text/event-stream",
            **self._authorization_headers(),
        }
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        if should_stop is not None and should_stop():
            return
        stream_timeout = self.timeout + max(0.0, float(timeout))
        try:
            with self._http.stream(
                "GET",
                f"{self.base_url}{self._v1_path('/events')}",
                params=params,
                headers=headers,
                timeout=stream_timeout,
            ) as response:
                if response.is_error:
                    raise _response_error(response)
                yield from self._iter_events(
                    _HttpxLineReader(response.iter_lines()),
                    should_stop=should_stop,
                )
        except IntelApiError:
            raise
        except httpx.TransportError as exc:
            raise _api_error(str(exc), transient=True) from exc


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


def _response_error(response: httpx.Response) -> IntelApiError:
    message = f"HTTP {response.status_code}"
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            message = str(body["error"])
    except ValueError:
        pass
    return _api_error(
        message,
        status_code=response.status_code,
        transient=response.status_code >= 500 or response.status_code == 429,
    )


class _HttpxLineReader:
    """Adapt httpx's line iterator to the urllib-style SSE parser interface."""

    def __init__(self, lines) -> None:
        self._lines = iter(lines)

    def readline(self):
        try:
            # httpx strips line endings from iter_lines(). Preserve one here so
            # the shared SSE parser can distinguish an empty separator from EOF.
            return f"{next(self._lines)}\n"
        except StopIteration:
            return ""
