from __future__ import annotations

import asyncio
import base64
import math
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from redis.asyncio import Redis

from eve_risk.clients.base import request_with_retries
from eve_risk.clients.qq_limits import QQProactiveBudget


class QQAPIError(RuntimeError):
    """Sanitized QQ API error that never includes user or group identifiers."""

    def __init__(
        self, message: str, *, http_status: int | None = None,
        code: int | None = None, trace_id: str = "", retry_after: float = 0.0,
        rate_limited: bool = False,
    ) -> None:
        self.http_status = http_status
        self.code = code
        self.trace_id = trace_id
        self.is_rate_limited = rate_limited or http_status == 429 or code in {40034100, 22009}
        self.retry_after = retry_after or (60.0 if self.is_rate_limited else 0.0)
        details = [f"status={http_status}", f"code={code}"]
        if trace_id:
            details.append(f"trace={trace_id}")
        super().__init__(f"{message} ({', '.join(details)})")


class QQOpenAPIClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        app_id: str,
        app_secret: str,
        token_url: str,
        api_base_url: str,
    ) -> None:
        self.http = http
        self.redis = redis
        self.app_id = app_id
        self.app_secret = app_secret
        self.token_url = token_url
        self.api_base_url = api_base_url.rstrip("/")
        self._token_lock = asyncio.Lock()
        self.proactive_budget = QQProactiveBudget(redis, app_id)
        self.rate_limit_generation = 0
        self._rate_limit_until = 0.0

    @property
    def retry_after_seconds(self) -> float:
        return max(0.0, self._rate_limit_until - time.monotonic())

    async def _record_rate_limit(self, group: str, error: QQAPIError, *, shared: bool) -> None:
        self.rate_limit_generation += 1
        self._rate_limit_until = max(self._rate_limit_until, time.monotonic() + error.retry_after)
        if shared:
            await self.proactive_budget.defer(group, error.retry_after)

    async def send_text(
        self, group_openid: str, msg_id: str, content: str, msg_seq: int
    ) -> dict[str, object]:
        return await self._post_message(
            group_openid,
            {
                "content": content,
                "msg_type": 0,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            },
        )

    async def send_proactive_text(
        self, group_openid: str, content: str
    ) -> dict[str, object]:
        return await self._post_message(
            group_openid,
            {
                "content": content,
                "msg_type": 0,
            },
        )

    async def send_proactive_markdown(
        self,
        group_openid: str,
        content: str,
        *,
        keyboard_id: str = "",
        keyboard_content: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "content": "",
            "msg_type": 2,
            "markdown": {"content": content},
        }
        if keyboard_id.strip():
            payload["keyboard"] = {"id": keyboard_id.strip()}
        elif keyboard_content:
            payload["keyboard"] = {"content": keyboard_content}
        return await self._post_message(
            group_openid,
            payload,
        )

    async def send_markdown(
        self,
        group_openid: str,
        msg_id: str,
        content: str,
        msg_seq: int,
        *,
        keyboard_id: str = "",
        keyboard_content: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "content": "",
            "msg_type": 2,
            "markdown": {"content": content},
            "msg_id": msg_id,
            "msg_seq": msg_seq,
        }
        if keyboard_id.strip():
            payload["keyboard"] = {"id": keyboard_id.strip()}
        elif keyboard_content:
            payload["keyboard"] = {"content": keyboard_content}
        return await self._post_message(
            group_openid,
            payload,
        )

    async def send_image(
        self, group_openid: str, msg_id: str, image_bytes: bytes, msg_seq: int
    ) -> dict[str, object]:
        file_info = await self._upload_image(group_openid, image_bytes)
        return await self._post_message(
            group_openid,
            {
                "content": "",
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            },
        )

    async def send_proactive_image(
        self, group_openid: str, image_bytes: bytes
    ) -> dict[str, object]:
        file_info = await self._upload_image(group_openid, image_bytes)
        return await self._post_message(
            group_openid,
            {
                "content": "",
                "msg_type": 7,
                "media": {"file_info": file_info},
            },
        )

    async def _upload_image(self, group_openid: str, image_bytes: bytes) -> object:
        token = await self._access_token()
        try:
            upload = await request_with_retries(
                self.http,
                "POST",
                f"{self.api_base_url}/v2/groups/{group_openid}/files",
                headers=self._headers(token),
                json={
                    "file_type": 1,
                    "file_data": base64.b64encode(image_bytes).decode("ascii"),
                },
                timeout=30.0,
            )
        except Exception:
            raise QQAPIError("QQ media upload failed") from None
        return upload.json()["file_info"]

    async def _post_message(
        self, group_openid: str, payload: dict[str, object]
    ) -> dict[str, object]:
        proactive = not (payload.get("msg_id") or payload.get("event_id"))
        if proactive:
            delay = await self.proactive_budget.reserve(group_openid)
            if delay > 0:
                error = QQAPIError(
                    "QQ proactive budget exhausted", retry_after=delay, rate_limited=True,
                )
                await self._record_rate_limit(group_openid, error, shared=False)
                raise error
        token = await self._access_token()
        try:
            # One reservation covers exactly one request. The consumer owns
            # retries; blind HTTP retries can duplicate a delivered message.
            response = await self.http.request(
                "POST",
                f"{self.api_base_url}/v2/groups/{group_openid}/messages",
                headers=self._headers(token),
                json=payload,
                timeout=10.0,
            )
        except Exception:
            raise QQAPIError("QQ group message request failed") from None
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = None
        raw_code = (
            response_payload.get("err_code", response_payload.get("code"))
            if isinstance(response_payload, dict) else None
        )
        if not response.is_success or (raw_code is not None and str(raw_code).strip() != "0"):
            try:
                code = int(raw_code) if raw_code is not None else None
            except (TypeError, ValueError):
                code = None
            trace = str(response.headers.get("X-Tps-trace-ID") or (
                response_payload.get("trace_id", "") if isinstance(response_payload, dict) else ""
            ))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", trace) or any(
                secret and secret in trace for secret in (group_openid, token, self.app_secret)
            ):
                trace = ""
            error = QQAPIError(
                "QQ group message rejected", http_status=response.status_code,
                code=code, trace_id=trace,
                retry_after=_retry_after(response.headers.get("Retry-After", "")),
            )
            if proactive and error.is_rate_limited:
                await self._record_rate_limit(group_openid, error, shared=True)
            raise error
        if not isinstance(response_payload, dict):
            raise QQAPIError("QQ group message returned an invalid response")
        return response_payload

    async def _access_token(self) -> str:
        cache_key = f"qq:access-token:{self.app_id}"
        cached = await self.redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)

        async with self._token_lock:
            cached = await self.redis.get(cache_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)
            response = await request_with_retries(
                self.http,
                "POST",
                self.token_url,
                json={"appId": self.app_id, "clientSecret": self.app_secret},
                timeout=10.0,
            )
            payload = response.json()
            token = str(payload["access_token"])
            expires_in = max(60, int(payload.get("expires_in", 7200)) - 60)
            await self.redis.set(cache_key, token, ex=expires_in)
            return token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.app_id,
            "Content-Type": "application/json",
            "X-Request-Timestamp": str(int(time.time())),
        }


def _retry_after(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
