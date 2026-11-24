"""Visual counts stay current without producing independent QQ messages."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from eve_risk.alerts import EveSentryAlertRelay

TIME = "2026-11-24T05:12:00Z"
SYSTEM = "S-KSWL"


@pytest_asyncio.fixture
async def relay_and_messages():
    messages: asyncio.Queue[str] = asyncio.Queue()

    async def send(_group, message):
        messages.put_nowait(message)

    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(side_effect=send),
        send_proactive_markdown=AsyncMock(side_effect=send),
    )
    async with fakeredis.aioredis.FakeRedis() as redis:
        relay = EveSentryAlertRelay(AsyncMock(), redis, qq, "https://example.invalid/events")
        await relay.subscribe("group")
        await relay.process_bootstrap({"active_intel": [], "generated_at": TIME})
        yield relay, messages


def alert(seq, count, **fields):
    return {
        "id": f"state:{seq}",
        "event_type": "alert.entered" if seq == 1 else "alert.updated",
        "created_at": TIME,
        "system_name": SYSTEM,
        "hostile_count": count,
        **fields,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["event", "snapshot"])
async def test_count_changes_are_silent(relay_and_messages, source, monkeypatch):
    relay, messages = relay_and_messages
    assert await relay.process_alert_event(alert(1, 2))
    assert messages.get_nowait() == "❗ S-KSWL 来敌"

    if source == "event":
        assert await relay.process_alert_event(alert(2, 4))
        assert await relay.process_alert_event(
            alert(3, 1, hostile_personnel=[], personnel_complete=False),
        )
    else:
        assert await relay.process_bootstrap({
            "active_intel": [],
            "generated_at": TIME,
            "hostile_personnel": [{
                "system_name": SYSTEM,
                "hostile_count": 1,
                "personnel": [],
                "personnel_complete": False,
            }],
        })

    states, _ = await relay._load_system_alert_state()
    assert states[SYSTEM.casefold()]["hostile_count"] == 1
    loop = asyncio.get_running_loop()
    time = loop.time
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: time() + 10)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(messages.get(), 0.3)

    assert await relay.process_safe_event(alert(4, 0, event_type="alert.cleared"))
    assert messages.get_nowait() == "✅ S-KSWL 清空"
    assert messages.empty()


@pytest.mark.asyncio
async def test_delayed_roster_uses_the_latest_visual_count(relay_and_messages):
    relay, messages = relay_and_messages
    relay.personnel_push_interval_seconds = 0.5

    assert await relay.process_alert_event(alert(1, 2))
    assert messages.get_nowait() == "❗ S-KSWL 来敌"
    assert await relay.process_alert_event(alert(2, 2, hostile_personnel=[{"name": "Pilot A"}]))
    assert "**敌对**｜2 人" in messages.get_nowait()

    assert await relay.process_alert_event(alert(3, 3, hostile_personnel=[{"name": "Pilot B"}]))
    assert await relay.process_alert_event(alert(4, 4))
    assert messages.empty()
    roster = await asyncio.wait_for(messages.get(), 2)
    assert "敌对事件" in roster
    assert "**敌对**｜4 人" in roster
    assert "Pilot B" in roster
    assert relay.qq.send_proactive_text.await_count == 1
