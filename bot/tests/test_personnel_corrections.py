"""ID-stable deduplication and empty roster delivery contracts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import EveSentryAlertRelay, _personnel_fingerprint, _personnel_removed


def test_renaming_same_id_does_not_change_fingerprint():
    assert _personnel_fingerprint([{"character_id": 1, "name": "Old", "system_name": "Tama"}]) == (
        _personnel_fingerprint([{"character_id": 1, "name": "New", "system_name": "Tama"}]))
    assert _personnel_fingerprint([{"character_id": 1, "name": "Old"}]) != (
        _personnel_fingerprint([{"character_id": 2, "name": "Old"}]))


@pytest.mark.asyncio
async def test_empty_correction_delivers_once_without_new_presence_alert():
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock(return_value={"id": "sent"}))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("test-group")
        state = {"system_name": "Tama", "episode_id": "wave-1", "hostile_count": 1,
                 "personnel": [], "personnel_fingerprint": "", "personnel_revision": 2,
                 "personnel_correction": True}
        assert await relay.deliver_system_personnel_update(state, "t1")
        assert await relay.deliver_system_personnel_update(state, "t1")
        assert qq.send_proactive_markdown.await_count == 1
        message = qq.send_proactive_markdown.await_args.args[1]
        assert "### ⚠️ 敌对事件" in message and "暂无已确认敌对" in message
        assert "人员名单更正" not in message
        assert "清空" not in message and "来敌" not in message
    await redis.aclose()


def test_partial_removal_is_correction_but_rename_is_not():
    before = [{"character_id": 1, "name": "A"}, {"character_id": 2, "name": "B"}]
    assert _personnel_removed(before, before[:1])
    assert not _personnel_removed(before[:1], [{"character_id": 1, "name": "Renamed"}])
    assert _personnel_fingerprint(before[:1]) == _personnel_fingerprint([before[0], before[0]])


@pytest.mark.asyncio
async def test_repeated_bootstrap_keeps_pending_empty_correction():
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock(return_value={"id": "sent"}),
                         send_proactive_text=AsyncMock(return_value={"id": "text"}))
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events",
                                    personnel_push_interval_seconds=0.05)
        await relay.subscribe("test-group")
        person = {"character_id": 1, "name": "A"}
        def bootstrap(personnel):
            return {"generated_at": "t1", "active_intel": [], "alerts": [],
                    "hostile_personnel": [{"system_name": "Tama", "hostile_count": 1, "personnel": personnel}]}
        await relay.process_bootstrap(bootstrap([person]))
        relay._personnel_last_sent_at["tama"] = asyncio.get_running_loop().time()
        await relay.process_bootstrap(bootstrap([]))
        assert "tama" in relay._personnel_pending
        await relay.process_bootstrap(bootstrap([]))
        assert "tama" in relay._personnel_pending
        await asyncio.sleep(0.1)
        assert qq.send_proactive_markdown.await_count == 1
        assert "### ⚠️ 敌对事件" in qq.send_proactive_markdown.await_args.args[1]
    await redis.aclose()
