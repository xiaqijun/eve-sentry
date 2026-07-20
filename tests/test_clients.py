import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import respx

from eve_risk.clients.base import request_with_retries
from eve_risk.clients.esi import ESIClient
from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.clients.zkill import ZKillClient
from eve_risk.ship_roles import ShipRoleClassifier


@pytest.mark.asyncio
async def test_qq_client_reuses_token_and_sends_media() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient() as http:
        client = QQOpenAPIClient(
            http,
            redis,
            "appid",
            "secret",
            "https://bots.qq.com/app/getAppAccessToken",
            "https://api.sgroup.qq.com",
        )
        with respx.mock(assert_all_called=True) as router:
            token = router.post("https://bots.qq.com/app/getAppAccessToken").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "token", "expires_in": "7200"}
                )
            )
            text = router.post("https://api.sgroup.qq.com/v2/groups/group/messages").mock(
                side_effect=[
                    httpx.Response(200, json={"id": "m1"}),
                    httpx.Response(200, json={"id": "m2"}),
                    httpx.Response(200, json={"id": "m3"}),
                ]
            )
            upload = router.post("https://api.sgroup.qq.com/v2/groups/group/files").mock(
                return_value=httpx.Response(200, json={"file_info": "file-token"})
            )

            await client.send_text("group", "source", "hello", 1)
            await client.send_proactive_text("group", "alert")
            await client.send_image("group", "source", b"png", 2)

            assert token.call_count == 1
            assert text.call_count == 3
            assert upload.call_count == 1
            proactive_body = json.loads(text.calls[1].request.content)
            assert proactive_body == {"content": "alert", "msg_type": 0}
            upload_body = json.loads(upload.calls[0].request.content)
            assert upload_body["file_data"] == "cG5n"


@pytest.mark.asyncio
async def test_zkill_client_parses_and_caches_payload() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    payload = [
        {
            "killmail_id": 123,
            "killmail_time": "2026-07-13T10:00:00Z",
            "solar_system_id": 30000142,
            "victim": {"character_id": 2, "ship_type_id": 1002},
            "attackers": [{"character_id": 1, "ship_type_id": 1001, "final_blow": True}],
            "zkb": {"solo": True, "totalValue": 12345.0},
        }
    ]
    async with httpx.AsyncClient() as http:
        client = ZKillClient(
            http, redis, "https://zkillboard.com/api", "Test contact@example.com", 0, 60
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.get("https://zkillboard.com/api/kills/characterID/1/").mock(
                return_value=httpx.Response(200, json=payload)
            )
            first = await client.fetch_character(1, "kills")
            second = await client.fetch_character(1, "kills")
            assert route.call_count == 1
            assert first.killmails[0].solo is True
            assert second.from_cache is True


@pytest.mark.asyncio
async def test_esi_resolves_only_exact_characters() -> None:
    async with httpx.AsyncClient() as http:
        client = ESIClient(http, "https://esi.evetech.net/latest", ShipRoleClassifier())
        with respx.mock(assert_all_called=True) as router:
            router.post("https://esi.evetech.net/latest/universe/ids/").mock(
                return_value=httpx.Response(
                    200, json={"characters": [{"id": 1, "name": "Alice Example"}]}
                )
            )
            router.get("https://esi.evetech.net/latest/characters/1/").mock(
                return_value=httpx.Response(200, json={"corporation_id": 101, "alliance_id": 201})
            )
            router.post("https://esi.evetech.net/latest/universe/names/").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {"id": 101, "name": "Alpha Corp", "category": "corporation"},
                        {"id": 201, "name": "Alliance One", "category": "alliance"},
                    ],
                )
            )
            identities, invalid = await client.resolve_characters(
                ["Alice Example", "Missing Pilot"]
            )
            assert identities[0].corporation_name == "Alpha Corp"
            assert identities[0].alliance_name == "Alliance One"
            assert invalid == ["Missing Pilot"]


@pytest.mark.asyncio
async def test_retry_only_for_retryable_status(monkeypatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("eve_risk.clients.base.asyncio.sleep", sleep)
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            retry_route = router.get("https://example.test/retry").mock(
                side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": True})]
            )
            response = await request_with_retries(http, "GET", "https://example.test/retry")
            assert response.json() == {"ok": True}
            assert retry_route.call_count == 2
            assert sleep.await_count == 1

        with respx.mock(assert_all_called=True) as router:
            bad_route = router.get("https://example.test/bad").mock(
                return_value=httpx.Response(400)
            )
            with pytest.raises(httpx.HTTPStatusError):
                await request_with_retries(http, "GET", "https://example.test/bad")
            assert bad_route.call_count == 1
