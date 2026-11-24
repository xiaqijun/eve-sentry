from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from eve_risk import alerts
from eve_risk.alerts import EveSentryAlertRelay
from eve_risk.clients.qq import QQAPIError

TIME = "2026-11-24T05:12:00Z"
SYSTEM = "S-KSWL"
COUNT_WINDOW = 0.1


@pytest_asyncio.fixture
async def relay_and_messages(monkeypatch):
    monkeypatch.setattr(alerts, "SYSTEM_COUNT_PUSH_INTERVAL_SECONDS", COUNT_WINDOW)
    messages: asyncio.Queue[str] = asyncio.Queue()

    async def send(_group, message):
        messages.put_nowait(message)

    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(side_effect=send),
        send_proactive_markdown=AsyncMock(),
    )
    redis = fakeredis.aioredis.FakeRedis()
    relay = EveSentryAlertRelay(AsyncMock(), redis, qq, "https://example.invalid/events")
    await relay.subscribe("group")
    await relay.process_bootstrap({"active_intel": [], "generated_at": TIME})
    yield relay, messages
    await relay._discard_system_count_update(SYSTEM.casefold())
    await redis.aclose()


def alert(seq, count, **kwargs):
    return {
        "id": f"state:{seq}",
        "event_type": "alert.entered" if seq == 1 else "alert.updated",
        "created_at": TIME,
        "system_name": SYSTEM,
        "hostile_count": count,
        **kwargs,
    }


@pytest.mark.asyncio
async def test_count_updates_coalesce_without_a_complete_roster(relay_and_messages):
    relay, messages = relay_and_messages
    await relay.process_alert_event(alert(1, 2))
    first = messages.get_nowait()
    # The first update may have no roster field. Later updates can carry an
    # empty or incomplete roster while the visual count is already known.
    await relay.process_alert_event(alert(2, 3))
    await relay.process_alert_event(alert(3, 4, hostile_personnel=[]))
    await relay.process_alert_event(
        alert(4, 5, hostile_personnel=[], personnel_complete=False)
    )
    assert messages.empty()
    update = await asyncio.wait_for(messages.get(), 1)
    assert first == "❗ S-KSWL 来敌 2 人"
    assert update == "⚠️ S-KSWL 敌对人数更新｜5 人"
    assert relay.qq.send_proactive_markdown.await_count == 0
    await relay.process_alert_event(alert(5, 5))
    await asyncio.sleep(COUNT_WINDOW * 2)
    assert messages.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["clear", "unavailable", "snapshot"])
async def test_pending_count_drops_when_the_incursion_ends(relay_and_messages, ending):
    relay, messages = relay_and_messages
    await relay.process_alert_event(alert(1, 2))
    messages.get_nowait()
    await relay.process_alert_event(alert(2, 4))
    if ending == "clear":
        await relay.process_safe_event(alert(3, 0))
    elif ending == "unavailable":
        await relay.process_alert_event(alert(3, 4, freshness="unknown"))
    else:
        await relay.process_bootstrap({"active_intel": [], "generated_at": TIME})
    if ending != "unavailable":
        assert messages.get_nowait() == "✅ S-KSWL 清空"
    await asyncio.sleep(COUNT_WINDOW * 2)
    assert messages.empty()


@pytest.mark.asyncio
async def test_snapshot_count_can_update_and_revert_without_extra_messages(relay_and_messages):
    relay, messages = relay_and_messages
    await relay.process_alert_event(alert(1, 2))
    messages.get_nowait()

    async def snapshot(count):
        await relay.process_bootstrap({
            "active_intel": [],
            "generated_at": TIME,
            "hostile_personnel": [{
                "system_name": SYSTEM,
                "hostile_count": count,
                "personnel": [],
                "personnel_complete": False,
            }],
        })

    await snapshot(4)
    await snapshot(2)
    await asyncio.sleep(COUNT_WINDOW * 2)
    assert messages.empty()
    await snapshot(6)
    assert await asyncio.wait_for(messages.get(), 1) == "⚠️ S-KSWL 敌对人数更新｜6 人"


@pytest.mark.asyncio
async def test_successful_roster_with_the_same_count_suppresses_duplicate_count(relay_and_messages):
    relay, messages = relay_and_messages
    await relay.process_alert_event(alert(1, 2))
    messages.get_nowait()
    await relay.process_alert_event(alert(2, 3, hostile_personnel=[{"name": "Pilot A"}]))
    assert relay.qq.send_proactive_markdown.await_count == 1
    assert "**敌对**｜3 人" in relay.qq.send_proactive_markdown.await_args.args[1]
    await asyncio.sleep(COUNT_WINDOW * 2)
    assert messages.empty()


@pytest.mark.asyncio
async def test_rate_limited_count_retries_as_latest_value(relay_and_messages):
    relay, messages = relay_and_messages
    await relay.process_alert_event(alert(1, 2))
    messages.get_nowait()
    send = relay.qq.send_proactive_text.side_effect
    first_attempt = asyncio.Event()
    relay.qq.retry_after_seconds = 0.25
    attempts = []

    async def limited_send(group, message):
        attempts.append(message)
        if len(attempts) == 1:
            first_attempt.set()
            raise QQAPIError("rate limited", rate_limited=True, retry_after=0.25)
        await send(group, message)

    relay.qq.send_proactive_text.side_effect = limited_send
    await relay.process_alert_event(alert(2, 3))
    await asyncio.wait_for(first_attempt.wait(), 1)
    await relay.process_alert_event(alert(3, 4))
    await asyncio.sleep(COUNT_WINDOW * 1.5)
    assert len(attempts) == 1
    assert await asyncio.wait_for(messages.get(), 1) == "⚠️ S-KSWL 敌对人数更新｜4 人"
    assert attempts == ["⚠️ S-KSWL 敌对人数更新｜3 人", "⚠️ S-KSWL 敌对人数更新｜4 人"]


@pytest.mark.asyncio
async def test_clear_waits_for_a_send_already_in_flight_and_drops_its_successor(relay_and_messages):
    relay, messages = relay_and_messages
    await relay.process_alert_event(alert(1, 2))
    messages.get_nowait()
    send = relay.qq.send_proactive_text.side_effect
    started = asyncio.Event()
    released = asyncio.Event()

    async def slow_send(group, message):
        if "人数更新" in message:
            started.set()
            await released.wait()
        await send(group, message)

    relay.qq.send_proactive_text.side_effect = slow_send
    await relay.process_alert_event(alert(2, 3))
    await asyncio.wait_for(started.wait(), 1)
    await relay.process_alert_event(alert(3, 4))

    clear = asyncio.create_task(relay.process_safe_event(alert(4, 0)))
    try:
        await asyncio.sleep(0)
        assert not clear.done() and messages.empty()
    finally:
        released.set()
        await asyncio.wait_for(clear, 1)

    assert messages.get_nowait() == "⚠️ S-KSWL 敌对人数更新｜3 人"
    assert messages.get_nowait() == "✅ S-KSWL 清空"
    await asyncio.sleep(COUNT_WINDOW * 2)
    assert messages.empty()


@pytest.mark.asyncio
async def test_delayed_roster_uses_the_latest_visual_count(relay_and_messages):
    relay, messages = relay_and_messages
    relay.personnel_push_interval_seconds = COUNT_WINDOW / 2

    async def send_roster(_group, message):
        messages.put_nowait(message)

    relay.qq.send_proactive_markdown.side_effect = send_roster
    await relay.process_alert_event(alert(1, 2))
    messages.get_nowait()
    await relay.process_alert_event(alert(2, 2, hostile_personnel=[{"name": "Pilot A"}]))
    assert "**敌对**｜2 人" in messages.get_nowait()
    await relay.process_alert_event(alert(3, 3, hostile_personnel=[{"name": "Pilot B"}]))
    await relay.process_alert_event(alert(4, 4))
    roster = await asyncio.wait_for(messages.get(), 1)
    assert "**敌对**｜4 人" in roster
    assert "Pilot B" in roster
    await asyncio.sleep(COUNT_WINDOW * 2)
    assert messages.empty()
