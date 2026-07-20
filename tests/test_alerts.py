from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import (
    ALERT_CURSOR_KEY,
    EveSentryAlertRelay,
    alert_subscription_action,
    format_alert_message,
    iter_sse_events,
)


async def _sse_lines():
    for line in (
        ": keepalive",
        "",
        "id: evt-1",
        "event: alert",
        'data: {"id":"evt-1"}',
        "",
    ):
        yield line


@pytest.mark.asyncio
async def test_iter_sse_events_ignores_comments_and_parses_alerts() -> None:
    events = [event async for event in iter_sse_events(_sse_lines())]

    assert events == [("alert", "evt-1", '{"id":"evt-1"}')]


def test_alert_subscription_commands_and_message_format() -> None:
    assert alert_subscription_action("开启预警") == "enable"
    assert alert_subscription_action("<@!bot> 关闭预警") == "disable"
    assert alert_subscription_action("预警状态") == "status"
    assert alert_subscription_action("分析 Alice") is None

    message = format_alert_message(
        {
            "id": "evt-1",
            "system_name": "S-KSWL",
            "names": ["Alice", "Bob"],
            "level": "high",
            "score": 80,
            "created_at": "2026-07-20T16:20:24+00:00",
        },
        "http://sentry.test/",
    )

    assert "【EVE Sentry 敌对预警】" in message
    assert "星系：S-KSWL" in message
    assert "目标：Alice、Bob" in message
    assert "等级：高（评分 80）" in message
    assert "时间：2026-07-21 00:20:24" in message
    assert "态势图：http://sentry.test" in message


@pytest.mark.asyncio
async def test_relay_subscribes_delivers_and_deduplicates_per_group() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_text=AsyncMock(return_value={"id": "m1"}))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/api/v1/events",
            public_url="http://sentry.test",
        )
        await relay.subscribe("group-1")
        assert await relay.is_subscribed("group-1") is True

        alert = {
            "id": "evt-1",
            "system_name": "S-KSWL",
            "names": ["Alice"],
            "level": "high",
            "score": 80,
            "created_at": "2026-07-20T16:20:24+00:00",
        }
        await relay.deliver(alert)
        await relay.deliver(alert)

        qq.send_proactive_text.assert_awaited_once()
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-07-20T16:20:24+00:00"

        await relay.unsubscribe("group-1")
        assert await relay.is_subscribed("group-1") is False

    await redis.aclose()
