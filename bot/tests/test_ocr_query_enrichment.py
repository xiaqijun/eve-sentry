from unittest.mock import AsyncMock

import botpy
import fakeredis.aioredis
import httpx
import pytest

from eve_risk.bot import RiskBotClient
from eve_risk.sentry_status import EveSentryStatusClient, format_ocr_query


def _payload(status: str, *, character_id: int | None = None, corporation: str = "") -> dict:
    return {
        "query_id": "query-1",
        "status": "completed",
        "expected_clients": 1,
        "results": [{
            "system_name": "R-YWID",
            "names": ["Sultanhatun"],
            "recognized": [{
                "name": "Sultanhatun",
                "character_id": character_id,
                "metadata": {"identity_status": status, "corporation_name": corporation},
            }],
        }],
    }


@pytest.mark.asyncio
async def test_waits_for_identity_and_returns_current_affiliation() -> None:
    requests = []
    ready = _payload("resolved", character_id=2124203775, corporation="InFlames.")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ready)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = EveSentryStatusClient(http, "http://sentry/api/v1/events")
        result = await client.wait_ocr_query_enrichment(
            {"query_id": "query-1"}, _payload("pending"), timeout_seconds=2
        )

    assert len(requests) == 1
    assert "InFlames." in format_ocr_query(result, {"mode": "all_nodes"})
    assert "资料未能及时补全" not in format_ocr_query(result, {"mode": "all_nodes"})


@pytest.mark.asyncio
async def test_waits_until_all_returned_nodes_are_enriched() -> None:
    def node(name: str, status: str, corporation: str = "") -> dict:
        return {
            "system_name": "R-YWID",
            "names": [name],
            "recognized": [{"name": name, "metadata": {
                "identity_status": status, "corporation_name": corporation,
            }}],
        }

    first = {"status": "completed", "results": [node("UnaiN7", "pending"), node("Sultanhatun", "pending")]}
    second = {"status": "completed", "results": [node("UnaiN7", "resolved", "Xa'sar"), node("Sultanhatun", "pending")]}
    third = {"status": "completed", "results": [node("UnaiN7", "resolved", "Xa'sar"), node("Sultanhatun", "resolved", "InFlames.")]}
    replies = iter([second, third])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(replies)))) as http:
        client = EveSentryStatusClient(http, "http://sentry/api/v1/events")
        result = await client.wait_ocr_query_enrichment({"query_id": "q"}, first, timeout_seconds=3)
    assert result == third


@pytest.mark.asyncio
async def test_pending_identity_returns_partial_with_explicit_notice_on_timeout() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(http, "http://sentry/api/v1/events")
        result = await client.wait_ocr_query_enrichment(
            {"query_id": "query-1"}, _payload("pending"), timeout_seconds=0
        )
    assert result["enrichment_incomplete"] is True
    assert "资料未能及时补全" in format_ocr_query(result, {"mode": "all_nodes"})
    assert "无法确认筛选结果" in format_ocr_query(
        result, {"mode": "filtered", "corporation": "InFlames."}
    )


@pytest.mark.asyncio
async def test_known_alliance_without_name_is_still_pending() -> None:
    partial = _payload("resolved", character_id=123, corporation="Glory Navy")
    partial["results"][0]["recognized"][0]["metadata"]["alliance_id"] = 456
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(http, "http://sentry/api/v1/events")
        result = await client.wait_ocr_query_enrichment(
            {"query_id": "query-1"}, partial, timeout_seconds=0
        )
    assert result["enrichment_incomplete"] is True


@pytest.mark.asyncio
async def test_empty_or_unresolvable_identity_does_not_wait() -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not poll")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        client = EveSentryStatusClient(http, "http://sentry/api/v1/events")
        for payload in (
            {"status": "completed", "results": [{"names": [], "recognized": []}]},
            _payload("unresolved"),
            _payload("resolved", character_id=123, corporation="Glory Navy"),
        ):
            assert await client.wait_ocr_query_enrichment({"query_id": "query-1"}, payload) == payload


@pytest.mark.asyncio
async def test_missing_rows_are_pending_and_network_failure_preserves_results() -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    partial = {"status": "completed", "results": [{"names": ["Sultanhatun"], "recognized": []}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        client = EveSentryStatusClient(http, "http://sentry/api/v1/events")
        result = await client.wait_ocr_query_enrichment({"query_id": "query-1"}, partial)
    assert result["results"] == partial["results"]
    assert result["enrichment_incomplete"] is True


@pytest.mark.asyncio
async def test_qq_query_formats_the_refreshed_snapshot() -> None:
    ready = _payload("resolved", character_id=2124203775, corporation="InFlames.")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=ready))
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    client.redis = fakeredis.aioredis.FakeRedis()
    client.sentry_status.http = httpx.AsyncClient(transport=transport)
    client.sentry_status.bootstrap_url = "http://sentry/api/v1/bootstrap"
    client.sentry_status.wait_ocr_query_payload = AsyncMock(return_value=_payload("pending"))
    client._send_query_markdown = AsyncMock()
    try:
        await client._finish_sentry_ocr_query(
            {"mode": "all_nodes"}, {"query_id": "query-1"},
            group_openid="group-1", lock_key="lock", lock_token="token",
        )
        message = client._send_query_markdown.await_args.args[1]
        assert "InFlames." in message
        assert "资料未能及时补全" not in message
    finally:
        await client.sentry_status.http.aclose()
        await client.http_client.aclose()
        await client.redis.aclose()
        await original_redis.aclose()
        await client.watch_engine.dispose()
