from __future__ import annotations

import asyncio
from collections.abc import Iterable

import httpx


class ExternalServiceError(RuntimeError):
    pass


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry_statuses: Iterable[int] = (420, 429, 500, 502, 503, 504),
    attempts: int = 3,
    **kwargs: object,
) -> httpx.Response:
    retry_statuses = set(retry_statuses)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            await asyncio.sleep(0.5 * (2**attempt))
            continue

        if response.status_code not in retry_statuses:
            response.raise_for_status()
            return response
        if attempt == attempts - 1:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2**attempt)
        reset = response.headers.get("X-Esi-Error-Limit-Reset")
        if response.status_code == 420 and reset and reset.isdigit():
            delay = max(delay, float(reset))
        await asyncio.sleep(min(delay, 10.0))
    raise ExternalServiceError(f"Request failed: {method} {url}") from last_error
