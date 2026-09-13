"""Canonical counts may decrease; unavailable capture is not an arrival."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from eve_risk.alerts import EveSentryAlertRelay


@pytest.mark.asyncio
async def test_unknown_event_is_acknowledged_without_delivery():
    state = {'s-kswl': {'hostile_count': 1}}
    discarded = []
    relay = SimpleNamespace(_active_alert_ids=set(), _advance_alert_cursor=AsyncMock(), _allows_transition=lambda _: True,
                            _load_system_alert_state=AsyncMock(return_value=(state, True)),
                            _save_system_alert_state=AsyncMock(), _discard_pending_personnel_update=discarded.append)
    assert await EveSentryAlertRelay.process_alert_event(relay, {
        "id": "state:2", "created_at": "2026-09-13T00:00:00Z", "system_name": "S-KSWL",
        "hostile_count": 1, "freshness": "unknown",
    })
    assert state == {}
    assert discarded == ['s-kswl']
    relay._save_system_alert_state.assert_awaited_once_with({})
    relay._advance_alert_cursor.assert_awaited_once()


@pytest.mark.asyncio
async def test_count_only_update_can_decrease_without_roster_spam():
    state = {"s-kswl": {"hostile_count": 7, "personnel": []}}
    relay = SimpleNamespace(
        _active_alert_ids=set(), _load_system_alert_state=AsyncMock(return_value=(state, True)),
        _save_system_alert_state=AsyncMock(), queue_system_personnel_update=AsyncMock(),
        _allows_transition=lambda _: True,
    )
    assert await EveSentryAlertRelay.process_alert_event(relay, {
        "id": "state:3", "created_at": "2026-09-13T00:00:00Z", "system_name": "S-KSWL",
        "event_type": "alert.updated", "hostile_count": 1, "hostile_personnel": [],
    })
    assert state["s-kswl"]["hostile_count"] == 1
    relay._save_system_alert_state.assert_awaited_once()
    relay.queue_system_personnel_update.assert_not_awaited()
