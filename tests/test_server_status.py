from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import ALERT_GROUPS_KEY
from eve_risk.server_status import EveServerStartupMonitor


def _response(start_time: str = "2026-08-27T11:00:00Z") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "players": 12345,
            "server_version": "3050123",
            "start_time": start_time,
            "vip": False,
        },
        request=httpx.Request("GET", "https://esi.evetech.net/status"),
    )


@pytest.mark.asyncio
async def test_online_startup_only_establishes_baseline() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    http = AsyncMock()
    http.get.return_value = _response()
    qq = AsyncMock()
    monitor = EveServerStartupMonitor(http, redis, qq, "https://esi.evetech.net/status")
    try:
        assert await monitor.check_once() is True
        qq.send_proactive_text.assert_not_awaited()
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_confirmed_downtime_then_startup_notifies_subscribed_groups_once() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group-1")
    http = AsyncMock()
    http.get.side_effect = [
        httpx.ConnectError("offline"),
        httpx.ConnectError("offline"),
        _response(),
        _response(),
    ]
    qq = AsyncMock()
    monitor = EveServerStartupMonitor(
        http,
        redis,
        qq,
        "https://esi.evetech.net/status",
        offline_threshold=2,
    )
    try:
        assert await monitor.check_once() is False
        assert await monitor.check_once() is False
        assert await monitor.check_once() is True
        assert await monitor.check_once() is True
        qq.send_proactive_text.assert_awaited_once()
        group, message = qq.send_proactive_text.await_args.args
        assert group == "group-1"
        assert "EVE 服务器已开服" in message
        assert "在线人数｜12345" in message
        assert "服务器版本｜3050123" in message
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_short_status_failure_does_not_create_startup_alert() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group-1")
    http = AsyncMock()
    http.get.side_effect = [httpx.ReadTimeout("slow"), _response()]
    qq = AsyncMock()
    monitor = EveServerStartupMonitor(
        http,
        redis,
        qq,
        "https://esi.evetech.net/status",
        offline_threshold=2,
    )
    try:
        assert await monitor.check_once() is False
        assert await monitor.check_once() is True
        qq.send_proactive_text.assert_not_awaited()
    finally:
        await redis.aclose()
