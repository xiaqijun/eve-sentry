"""QQ's proactive quotas must survive restarts and concurrent senders."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.clients.qq import QQAPIError, QQOpenAPIClient, _retry_after
from eve_risk.clients.qq_limits import QQProactiveBudget


@pytest.mark.asyncio
async def test_shared_budget_limits_group_and_recovers_after_a_minute():
    redis = fakeredis.aioredis.FakeRedis()
    now = [1000.0]
    first = QQProactiveBudget(redis, "app", clock=lambda: now[0])
    second = QQProactiveBudget(redis, "app", clock=lambda: now[0])
    try:
        assert [await first.reserve("group") for _ in range(20)] == [0.0] * 20
        assert await second.reserve("group") > 60
        now[0] += 61.1
        assert await second.reserve("group") == 0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_concurrent_senders_cannot_overbook_group_quota():
    redis = fakeredis.aioredis.FakeRedis()
    clients = [QQProactiveBudget(redis, "app") for _ in range(3)]
    try:
        delays = await asyncio.gather(*(clients[i % 3].reserve("group") for i in range(45)))
        assert 0 < delays.count(0) <= 20
        assert await clients[0].reserve("other-group") == 0
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_bot_budget_is_shared_across_groups():
    redis = fakeredis.aioredis.FakeRedis()
    budget = QQProactiveBudget(redis, "app")
    try:
        for i in range(30):
            assert await budget.reserve(f"group-{i}") == 0
        assert await budget.reserve("next-group") > 60
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,field,code", [(400, "err_code", 40034100),
                                               (429, "code", 22009),
                                               (200, "err_code", 40034100)])
async def test_rate_error_retains_safe_metadata_and_blocks_immediate_fallback(status, field, code):
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set("qq:access-token:app", "private-token")
    requests = []

    def limited(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "0.05", "X-Tps-trace-ID": "test-trace"},
                              json={field: code, "message": "private-token private-group private-secret"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as http:
            qq = QQOpenAPIClient(http, redis, "app", "private-secret", "https://qq.test/token", "https://qq.test")
            with pytest.raises(QQAPIError) as caught:
                await qq.send_proactive_markdown("private-group", "test")
            error = caught.value
            assert error.code == code and error.http_status == status
            assert error.trace_id == "test-trace" and error.retry_after == 0.05
            assert error.is_rate_limited and qq.retry_after_seconds > 0
            assert str(code) in str(error)
            assert not any(value in str(error) for value in ("private-token", "private-group", "private-secret"))
            with pytest.raises(QQAPIError) as fallback:
                await qq.send_proactive_text("private-group", "fallback")
            assert fallback.value.is_rate_limited
            assert len(requests) == 1
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_passive_reply_still_sends_when_proactive_group_budget_is_full():
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set("qq:access-token:app", "token")
    requests = []

    def accepted(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "accepted"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(accepted)) as http:
            qq = QQOpenAPIClient(http, redis, "app", "secret", "https://qq.test/token", "https://qq.test")
            for _ in range(20):
                await qq.send_proactive_text("group", "test")
            with pytest.raises(QQAPIError):
                await qq.send_proactive_text("group", "deferred")
            assert await qq.send_text("group", "source-id", "reply", 1) == {"id": "accepted"}
            assert len(requests) == 21
    finally:
        await redis.aclose()


@pytest.mark.parametrize("value", ["", "NaN", "inf", "-1", "nonsense"])
def test_invalid_retry_after_does_not_create_an_unbounded_sleep(value):
    assert _retry_after(value) == 0


def test_retry_after_accepts_http_date():
    value = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 58 < _retry_after(value) <= 60
