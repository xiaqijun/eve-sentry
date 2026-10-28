"""Startup mentions should not suppress the announcement when unavailable."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import ALERT_GROUPS_KEY
from eve_risk.clients.qq import QQAPIError, QQOpenAPIClient
from eve_risk.server_status import EveServerStartupMonitor, EveServerStatus


@pytest.mark.asyncio
async def test_startup_mentions_everyone_once_per_event():
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group")
    qq = AsyncMock()
    monitor = EveServerStartupMonitor(AsyncMock(), redis, qq, "https://esi.example/status")
    status = EveServerStatus(1, "test", datetime(2026, 10, 28, 11, tzinfo=UTC), False)
    try:
        await monitor._deliver_startup(status)
        await monitor._deliver_startup(status)
        qq.send_proactive_text.assert_awaited_once()
        assert "<qqbot-at-everyone />" in qq.send_proactive_text.await_args.args[1]
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [200, 400, 403])
async def test_startup_falls_back_when_everyone_mention_is_rejected(http_status):
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group")
    qq = AsyncMock()
    error = QQAPIError("rejected")
    error.http_status = http_status
    error.code = 40054001
    error.is_rate_limited = False
    qq.send_proactive_text.side_effect = [error, {"id": "ok"}]
    monitor = EveServerStartupMonitor(AsyncMock(), redis, qq, "https://esi.example/status")
    status = EveServerStatus(1, "test", datetime(2026, 10, 28, 11, tzinfo=UTC), False)
    try:
        await monitor._deliver_startup(status)
        assert qq.send_proactive_text.await_count == 2
        first, fallback = qq.send_proactive_text.await_args_list
        assert "<qqbot-at-everyone />" in first.args[1]
        assert "<qqbot-at-everyone />" not in fallback.args[1]
        await monitor._deliver_startup(status)
        assert qq.send_proactive_text.await_count == 2
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_rate_limit_does_not_trigger_an_immediate_fallback():
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group")
    qq = AsyncMock()
    error = QQAPIError("rate limited")
    error.http_status = 429
    error.is_rate_limited = True
    qq.send_proactive_text.side_effect = error
    monitor = EveServerStartupMonitor(AsyncMock(), redis, qq, "https://esi.example/status")
    status = EveServerStatus(1, "test", datetime(2026, 10, 28, 11, tzinfo=UTC), False)
    try:
        await monitor._deliver_startup(status)
        qq.send_proactive_text.assert_awaited_once()
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_failed_startup_is_retried_on_next_status_check():
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group")
    http = AsyncMock()
    http.get.side_effect = [
        httpx.Response(200, json={"start_time": "2026-10-27T11:00:00Z"},
                       request=httpx.Request("GET", "https://esi.example/status")),
        httpx.Response(200, json={"start_time": "2026-10-28T11:00:00Z"},
                       request=httpx.Request("GET", "https://esi.example/status")),
        httpx.Response(200, json={"start_time": "2026-10-28T11:00:00Z"},
                       request=httpx.Request("GET", "https://esi.example/status")),
        httpx.Response(200, json={"start_time": "2026-10-28T11:00:00Z"},
                       request=httpx.Request("GET", "https://esi.example/status")),
    ]
    qq = AsyncMock()
    error = QQAPIError("rate limited")
    error.http_status = 429
    error.is_rate_limited = True
    qq.send_proactive_text.side_effect = [error, {"id": "ok"}]
    monitor = EveServerStartupMonitor(http, redis, qq, "https://esi.example/status")
    try:
        await monitor.check_once()  # Establish an online baseline.
        await monitor.check_once()  # A new startup cannot yet be delivered.
        assert qq.send_proactive_text.await_count == 1
        await monitor.check_once()  # Retry from the persisted pending state.
        assert qq.send_proactive_text.await_count == 2
        await monitor.check_once()
        assert qq.send_proactive_text.await_count == 2
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_startup_mention_reaches_qq_text_payload():
    redis = fakeredis.aioredis.FakeRedis()
    await redis.sadd(ALERT_GROUPS_KEY, "group")
    await redis.set("qq:access-token:app", "token")
    requests = []

    def accept(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "accepted"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(accept)) as http:
            qq = QQOpenAPIClient(http, redis, "app", "secret", "https://qq.test/token", "https://qq.test")
            monitor = EveServerStartupMonitor(http, redis, qq, "https://esi.example/status")
            status = EveServerStatus(1, "test", datetime(2026, 10, 28, 11, tzinfo=UTC), False)
            assert await monitor._deliver_startup(status)
            assert len(requests) == 1
            import json
            payload = json.loads(requests[0].content)
            assert payload["msg_type"] == 0
            assert "<qqbot-at-everyone />" in payload["content"]
    finally:
        await redis.aclose()
