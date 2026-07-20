from __future__ import annotations

import base64
import time

import httpx
from redis.asyncio import Redis

from eve_risk.clients.base import request_with_retries


class QQAPIError(RuntimeError):
    """Sanitized QQ API error that never includes user or group identifiers."""


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

    async def send_image(
        self, group_openid: str, msg_id: str, image_bytes: bytes, msg_seq: int
    ) -> dict[str, object]:
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
        file_info = upload.json()["file_info"]
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

    async def _post_message(
        self, group_openid: str, payload: dict[str, object]
    ) -> dict[str, object]:
        token = await self._access_token()
        try:
            response = await request_with_retries(
                self.http,
                "POST",
                f"{self.api_base_url}/v2/groups/{group_openid}/messages",
                headers=self._headers(token),
                json=payload,
                timeout=10.0,
            )
        except Exception:
            raise QQAPIError("QQ group message request failed") from None
        return response.json()

    async def _access_token(self) -> str:
        cache_key = f"qq:access-token:{self.app_id}"
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
