import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import respx

from eve_risk.clients.base import request_with_retries
from eve_risk.clients.esi import ESIClient
from eve_risk.clients.images import EveImageClient
from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.clients.zkill import ZKillClient
from eve_risk.domain import LatestEngagement, RelatedBattleRef
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
                    httpx.Response(200, json={"id": "m4"}),
                ]
            )
            upload = router.post("https://api.sgroup.qq.com/v2/groups/group/files").mock(
                return_value=httpx.Response(200, json={"file_info": "file-token"})
            )

            await client.send_text("group", "source", "hello", 1)
            await client.send_proactive_text("group", "alert")
            await client.send_proactive_markdown("group", "**alert**")
            await client.send_image("group", "source", b"png", 2)

            assert token.call_count == 1
            assert text.call_count == 4
            assert upload.call_count == 1
            proactive_body = json.loads(text.calls[1].request.content)
            assert proactive_body == {"content": "alert", "msg_type": 0}
            markdown_body = json.loads(text.calls[2].request.content)
            assert markdown_body == {
                "content": "",
                "msg_type": 2,
                "markdown": {"content": "**alert**"},
            }
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
            stats_route = router.get(
                "https://zkillboard.com/api/stats/characterID/1/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "shipsDestroyed": 1099,
                        "shipsLost": 37,
                        "pointsDestroyed": 2132,
                        "iskDestroyed": 202_342_000_000,
                        "iskLost": 3_640_000_000,
                        "soloKills": 40,
                        "dangerRatio": 92,
                        "gangRatio": 97,
                    },
                )
            )
            first = await client.fetch_character(1, "kills")
            second = await client.fetch_character(1, "kills")
            stats = await client.fetch_character_stats(1)
            cached_stats = await client.fetch_character_stats(1)
            assert route.call_count == 1
            assert stats_route.call_count == 1
            assert first.killmails[0].solo is True
            assert second.from_cache is True
            assert stats == cached_stats
            assert stats.ships_destroyed == 1099
            assert stats.points_destroyed == 2132
            assert stats.danger_ratio == 92
            assert stats.gang_ratio == 97


@pytest.mark.asyncio
async def test_zkill_related_battle_enriches_fleet_values() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    occurred_at = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)
    engagement = LatestEngagement(
        started_at=occurred_at,
        last_seen=occurred_at,
        solar_system_id=30000241,
        system_name="YMJG-4",
        fleet_size=1,
        event_count=1,
        related_battle_refs=[
            RelatedBattleRef(system_id=30000241, occurred_at=occurred_at)
        ],
    )
    payload = {
        "summary": {
            "teamA": {
                "list": [
                    {"characterID": 1, "shipTypeID": 1001, "isVictim": False},
                    {
                        "characterID": 2,
                        "shipTypeID": 1002,
                        "shipName": "Friendly Loss",
                        "isVictim": True,
                    },
                ],
                "totals": {
                    "total_price": 420_000_000,
                    "totalShips": 1,
                    "pilotCount": 20,
                },
            },
            "teamB": {
                "list": [
                    {
                        "characterID": 3,
                        "shipTypeID": 1003,
                        "shipName": "Enemy Loss",
                        "isVictim": True,
                    }
                ],
                "totals": {
                    "total_price": 1_139_000_000,
                    "totalShips": 1,
                    "pilotCount": 15,
                },
            },
        }
    }
    async with httpx.AsyncClient() as http:
        client = ZKillClient(
            http, redis, "https://zkillboard.com/api", "Test contact@example.com", 0, 60
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.get(
                "https://zkillboard.com/api/related/30000241/202607161800/"
            ).mock(return_value=httpx.Response(200, json=payload))

            first = await client.enrich_related_battles([engagement], {1})
            second = await client.enrich_related_battles([engagement], {1})

            assert route.call_count == 1
            assert first == second
            assert first[0].lost_value == 420_000_000
            assert first[0].destroyed_value == 1_139_000_000
            assert first[0].fleet_size == 2
            assert first[0].lost_ships[0].id == 1002
            assert first[0].destroyed_ships[0].id == 1003


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
                return_value=httpx.Response(
                    200,
                    json={
                        "corporation_id": 101,
                        "alliance_id": 201,
                        "birthday": "2020-01-02T03:04:05Z",
                        "security_status": 4.1,
                    },
                )
            )
            router.get("https://esi.evetech.net/latest/corporations/101/").mock(
                return_value=httpx.Response(
                    200, json={"name": "Alpha Corp", "ticker": "ALPHA"}
                )
            )
            router.get("https://esi.evetech.net/latest/alliances/201/").mock(
                return_value=httpx.Response(
                    200, json={"name": "Alliance One", "ticker": "ONE"}
                )
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
            assert identities[0].corporation_ticker == "ALPHA"
            assert identities[0].alliance_name == "Alliance One"
            assert identities[0].alliance_ticker == "ONE"
            assert identities[0].birthday.isoformat() == "2020-01-02T03:04:05+00:00"
            assert identities[0].security_status == 4.1
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


@pytest.mark.asyncio
async def test_eve_image_client_fetches_and_caches_assets() -> None:
    async with httpx.AsyncClient() as http:
        client = EveImageClient(http, "https://images.evetech.net")
        with respx.mock(assert_all_called=True) as router:
            portrait = router.get(
                "https://images.evetech.net/characters/1/portrait"
            ).mock(return_value=httpx.Response(200, content=b"portrait", headers={"content-type": "image/jpeg"}))
            ship = router.get("https://images.evetech.net/types/1001/icon").mock(
                return_value=httpx.Response(200, content=b"ship", headers={"content-type": "image/png"})
            )

            corporation = router.get(
                "https://images.evetech.net/corporations/101/logo"
            ).mock(return_value=httpx.Response(200, content=b"corp", headers={"content-type": "image/png"}))
            alliance = router.get(
                "https://images.evetech.net/alliances/201/logo"
            ).mock(return_value=httpx.Response(200, content=b"alliance", headers={"content-type": "image/png"}))

            portraits, ships, corporations, alliances = await client.fetch_report_assets(
                [1], [1001], [101], [201]
            )
            cached_portraits, cached_ships, cached_corporations, cached_alliances = (
                await client.fetch_report_assets([1], [1001], [101], [201])
            )

            assert portraits == cached_portraits == {1: b"portrait"}
            assert ships == cached_ships == {1001: b"ship"}
            assert corporations == cached_corporations == {101: b"corp"}
            assert alliances == cached_alliances == {201: b"alliance"}
            assert portrait.call_count == 1
            assert ship.call_count == 1
            assert corporation.call_count == 1
            assert alliance.call_count == 1
