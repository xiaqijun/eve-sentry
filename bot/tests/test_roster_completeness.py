"""Do not notify a departure and return when classification is only temporarily unknown."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import EveSentryAlertRelay


def bootstrap(personnel, complete=True):
    return {"generated_at": "t1", "active_intel": [], "alerts": [], "hostile_personnel": [{
        "system_name": "Tama", "hostile_count": 1, "personnel": personnel,
        "personnel_complete": complete,
    }]}


@pytest.mark.asyncio
async def test_queued_update_retries_a_temporary_state_read_failure():
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock(return_value={"id": "sent"}))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events",
                                    personnel_push_interval_seconds=0.01)
        await relay.subscribe("test-group")
        state = {"system_name": "Tama", "episode_id": "wave", "hostile_count": 1,
                 "personnel": [{"character_id": 1, "name": "Pilot"}], "personnel_revision": 1,
                 "personnel_fingerprint": "confirmed-pilot"}
        relay._personnel_pending["tama"] = (state, "t1")
        relay._load_system_alert_state = AsyncMock(side_effect=[
            RuntimeError("temporarily unavailable"), ({"tama": state}, True),
        ])
        await asyncio.wait_for(relay._flush_personnel_update("tama", 0), 1)
        assert qq.send_proactive_markdown.await_count == 1
        assert relay._personnel_pending == {}
    await redis.aclose()


async def update(relay, lane, personnel, complete=True, seq=2):
    if lane == "bootstrap":
        return await relay.process_bootstrap(bootstrap(personnel, complete))
    return await relay.process_alert_event({
        "id": f"state:{seq}", "event_type": "alert.updated", "created_at": "t1",
        "system_name": "Tama", "hostile_count": 1, "hostile_personnel": personnel,
        "personnel_complete": complete,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["bootstrap", "event"])
@pytest.mark.parametrize("restart", [False, True])
async def test_pending_then_same_roster_does_not_send_a_correction_or_repeat(lane, restart):
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock(return_value={"id": "sent"}))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("test-group")
        person = {"character_id": 1, "name": "Pilot"}
        await relay.process_bootstrap(bootstrap([person]))
        assert await update(relay, lane, [], False)
        # Current queries must never read the remembered notification baseline.
        assert await relay.current_analysis_names() == []
        assert qq.send_proactive_markdown.await_count == 0
        if restart:
            relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        assert await update(relay, lane, [person], True, seq=3)
        assert await relay.current_analysis_names() == ["Pilot"]
        assert qq.send_proactive_markdown.await_count == 0
        # A genuinely confirmed removal must still notify immediately.
        assert await update(relay, lane, [], True, seq=4)
        assert qq.send_proactive_markdown.await_count == 1
    await redis.aclose()


@pytest.mark.asyncio
async def test_pending_classification_defers_but_does_not_lose_an_already_queued_roster():
    redis = fakeredis.aioredis.FakeRedis()
    sent = asyncio.Event()

    async def delivered(*_):
        sent.set()
        return {"id": "sent"}

    qq = SimpleNamespace(send_proactive_markdown=AsyncMock(side_effect=delivered))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events",
                                    personnel_push_interval_seconds=0.03)
        await relay.subscribe("test-group")
        person = {"character_id": 1, "name": "Pilot"}
        await relay.process_bootstrap(bootstrap([]))
        relay._personnel_last_sent_at["tama"] = asyncio.get_running_loop().time()
        await update(relay, "event", [person], True)
        assert "tama" in relay._personnel_pending
        await update(relay, "event", [], False, seq=3)
        assert "tama" in relay._personnel_pending
        await asyncio.sleep(0.07)
        assert not sent.is_set()
        await update(relay, "event", [person], True, seq=4)
        await asyncio.wait_for(sent.wait(), 1)
        assert qq.send_proactive_markdown.await_count == 1
        await asyncio.gather(*relay._personnel_flush_tasks.values())
    await redis.aclose()


@pytest.mark.asyncio
async def test_visual_clear_is_not_deferred_by_incomplete_personnel():
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock(return_value={"id": "sent"}),
                         send_proactive_text=AsyncMock(return_value={"id": "text"}))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("test-group")
        await relay.process_bootstrap(bootstrap([{"character_id": 1, "name": "Pilot"}]))
        await update(relay, "event", [], False)
        qq.send_proactive_markdown.reset_mock()
        assert await relay.process_safe_event({"id": "state:3", "system_name": "Tama", "created_at": "t2"})
        states, _ = await relay._load_system_alert_state()
        assert states == {}
        assert qq.send_proactive_markdown.await_count + qq.send_proactive_text.await_count == 1
    await redis.aclose()
