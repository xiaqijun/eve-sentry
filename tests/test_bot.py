from unittest.mock import AsyncMock

import botpy
import fakeredis.aioredis
import httpx
import pytest

from eve_risk.bot import RiskBotClient


@pytest.mark.asyncio
async def test_custom_http_client_does_not_replace_botpy_login_client() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    try:
        assert hasattr(client.http, "login")
        assert isinstance(client.http_client, httpx.AsyncClient)
    finally:
        await client.http_client.aclose()
        await client.redis.aclose()


@pytest.mark.asyncio
async def test_group_can_enable_and_disable_proactive_alerts() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.alert_relay.redis = redis
    client.alert_relay.events_url = "http://sentry.test/api/v1/events"
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "message-1"
        group_openid = "group-1"
        content = "开启预警"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())
        assert await client.alert_relay.is_subscribed("group-1") is True
        client.qq.send_text.assert_awaited_once_with(
            "group-1",
            "message-1",
            "已开启 EVE Sentry 主动预警，新敌对告警会推送到本群。",
            msg_seq=1,
        )

        Message.id = "message-2"
        Message.content = "关闭预警"
        await client.on_group_at_message_create(Message())
        assert await client.alert_relay.is_subscribed("group-1") is False
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()
