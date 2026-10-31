from types import SimpleNamespace
from unittest.mock import AsyncMock

import botpy
import fakeredis.aioredis
import httpx
import pytest
from botpy.connection import ConnectionState
from botpy.message import GroupMessage

from eve_risk.admission import AdmissionResult
from eve_risk.bot import RiskBotClient


@pytest.mark.asyncio
async def test_full_group_event_dispatches_only_a_mention_of_this_bot(monkeypatch):
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    client.redis = fakeredis.aioredis.FakeRedis()
    client.admission.admit = AsyncMock(return_value=AdmissionResult.OK)
    client.queue.enqueue = AsyncMock()
    client.qq._access_token = AsyncMock(return_value="token")
    request = httpx.Request("GET", "https://example.test/bot_state")
    response = httpx.Response(200, json={"member_openid": "bot-member"}, request=request)
    client.http_client.get = AsyncMock(return_value=response)
    dispatched = []
    state = ConnectionState(lambda name, *args: dispatched.append((name, args)), client.api)

    async def fake_login(self, token):
        self._connection = SimpleNamespace(state=state)

    monkeypatch.setattr(botpy.Client, "_bot_login", fake_login)
    try:
        await client._bot_login(None)
        parser = state.parsers["group_message_create"]
        data = {"id": "message-1", "group_openid": "group-1", "author": {"member_openid": "user-1"}, "content": "<@bot-member> /分析 ballsack brinkrns"}
        parser({"d": data})
        assert len(dispatched) == 1
        event, args = dispatched.pop()
        assert event == "group_full_message_create"
        await client.on_group_full_message_create(*args)
        client.queue.enqueue.assert_awaited_once()
        assert client.queue.enqueue.await_args.args[0].character_names == ["ballsack brinkrns"]

        await client.on_group_full_message_create(data)
        await client.on_group_at_message_create(GroupMessage(client.api, None, data))
        client.queue.enqueue.assert_awaited_once()

        for content in ("/分析 ballsack brinkrns", "<@other> /分析 ballsack brinkrns", "<@all> /分析 ballsack brinkrns"):
            await client.on_group_full_message_create({**data, "id": content, "content": content})
        client.queue.enqueue.assert_awaited_once()
        client.http_client.get.assert_awaited_once()
    finally:
        await client.http_client.aclose()
        await client.redis.aclose()
        await original_redis.aclose()
        await client.watch_engine.dispose()


@pytest.mark.asyncio
async def test_full_group_identity_lookup_failure_does_not_handle_message():
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    client.qq._access_token = AsyncMock(return_value="token")
    client.http_client.get = AsyncMock(side_effect=httpx.ConnectError("offline"))
    client.on_group_at_message_create = AsyncMock()
    try:
        await client.on_group_full_message_create({"id": "m", "group_openid": "g", "content": "<@bot> /分析 Alice", "author": {"member_openid": "u"}})
        client.on_group_at_message_create.assert_not_awaited()
    finally:
        await client.http_client.aclose()
        await client.redis.aclose()
        await client.watch_engine.dispose()
