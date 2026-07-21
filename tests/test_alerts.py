from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import (
    ACTIVE_INTEL_STATE_KEY,
    ALERT_CURSOR_KEY,
    EveSentryAlertRelay,
    alert_subscription_action,
    format_active_intel_message,
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

    item = {
        "id": "ocr:alice",
        "system_name": "S-KSWL",
        "name": "Alice",
        "source": "eve-sentry-detector",
        "source_instance": "EVE - Hajimi6",
        "level": "high",
        "score": 80,
        "first_seen_at": "2026-07-20T16:20:24+00:00",
        "metadata": {
            "alliance_ticker": "FRT",
            "alliance_name": "Fraternity.",
            "corporation_ticker": "G.N.V",
            "corporation_name": "Glory Navy",
        },
    }
    entered = format_active_intel_message(
        item,
        "entered",
        "2026-07-20T16:20:24+00:00",
        "http://sentry.test/",
    )
    left = format_active_intel_message(
        item,
        "left",
        "2026-07-20T16:22:29+00:00",
        "http://sentry.test/",
    )

    assert "### 🔴 敌对进入" in entered
    assert "**位置**｜S-KSWL" in entered
    assert "**目标**｜Alice" in entered
    assert "**势力**｜[FRT] Fraternity. / [G.N.V] Glory Navy" in entered
    assert "**威胁**｜高（评分 80）" in entered
    assert "**来源**｜OCR 监控 · EVE - Hajimi6" in entered
    assert "**进入时间**｜2026-07-21 00:20:24" in entered
    assert "状态" not in entered
    assert "态势图" not in entered
    assert "### 🟢 敌对离开" in left
    assert "**离开时间**｜2026-07-21 00:22:29" in left
    assert "**停留**｜2 分 5 秒" in left
    assert "状态" not in left
    assert "态势图" not in left
    assert "敌对进入" in format_alert_message(item)


@pytest.mark.asyncio
async def test_relay_pushes_one_enter_and_one_leave_per_active_target() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "m1"}),
        send_proactive_text=AsyncMock(return_value={"id": "m2"}),
    )
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

        item = {
            "id": "ocr:alice",
            "active": True,
            "system_name": "S-KSWL",
            "name": "Alice",
            "source": "eve-sentry-detector",
            "source_instance": "EVE - Hajimi6",
            "first_seen_at": "2026-07-20T16:20:24+00:00",
            "last_seen_at": "2026-07-20T16:20:24+00:00",
        }
        second_item = {
            **item,
            "id": "ocr:bob",
            "name": "Bob",
        }
        entered_bootstrap = {
            "generated_at": "2026-07-20T16:20:24+00:00",
            "active_intel": [
                item,
                second_item,
                {
                    **item,
                    "id": "ocr:friendly",
                    "name": "Friendly Pilot",
                },
            ],
            "alerts": [
                {
                    "active_intel_id": "ocr:alice",
                    "level": "high",
                    "score": 80,
                },
                {
                    "active_intel_id": "ocr:bob",
                    "level": "medium",
                    "score": 55,
                },
            ],
        }
        refreshed_bootstrap = {
            **entered_bootstrap,
            "generated_at": "2026-07-20T16:20:30+00:00",
            "active_intel": [
                {**item, "last_seen_at": "2026-07-20T16:20:30+00:00"},
                {**second_item, "last_seen_at": "2026-07-20T16:20:30+00:00"},
            ],
        }
        left_bootstrap = {
            "generated_at": "2026-07-20T16:22:29+00:00",
            "active_intel": [],
            "alerts": [],
        }

        await relay.process_bootstrap(entered_bootstrap)
        await relay.process_bootstrap(refreshed_bootstrap)
        await relay.process_bootstrap(left_bootstrap)
        await relay.process_bootstrap(left_bootstrap)

        assert qq.send_proactive_markdown.await_count == 2
        qq.send_proactive_text.assert_not_awaited()
        entered_message = qq.send_proactive_markdown.await_args_list[0].args[1]
        left_message = qq.send_proactive_markdown.await_args_list[1].args[1]
        assert "敌对进入" in entered_message
        assert "敌对进入监控" not in entered_message
        assert "**目标**｜Alice、Bob" in entered_message
        assert "敌对离开" in left_message
        assert "敌对离开监控" not in left_message
        assert "**目标**｜Alice、Bob" in left_message
        assert "**最长停留**｜2 分 5 秒" in left_message
        assert await redis.hlen(ACTIVE_INTEL_STATE_KEY) == 0
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-07-20T16:22:29+00:00"

        await relay.unsubscribe("group-1")
        assert await relay.is_subscribed("group-1") is False

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_falls_back_to_plain_text_when_markdown_is_unavailable() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(side_effect=RuntimeError("unsupported")),
        send_proactive_text=AsyncMock(return_value={"id": "m1"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.deliver(
            {
                "id": "ocr:alice",
                "system_name": "S-KSWL",
                "name": "Alice",
                "first_seen_at": "2026-07-20T16:20:24+00:00",
            },
            "entered",
            "2026-07-20T16:20:24+00:00",
        )

        fallback_message = qq.send_proactive_text.await_args.args[1]
        assert fallback_message.startswith("🔴 敌对进入")
        assert "**" not in fallback_message

    await redis.aclose()
