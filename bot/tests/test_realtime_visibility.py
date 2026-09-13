"""Unavailable state must neither retain a roster nor synthesize a clear."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import EveSentryAlertRelay, format_monitoring_nodes_message


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_unknown', [False, True])
async def test_missing_monitor_is_removed_without_safe_or_personnel(legacy_unknown):
    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, SimpleNamespace(), 'http://unused.test/events')
        await relay._save_system_alert_state({'tama': {'system_name': 'Tama', 'hostile_count': 2,
            'episode_id': 'old', 'personnel': [{'character_id': 1, 'name': 'Old'}]}})
        relay.deliver_system_transition = AsyncMock(return_value=True)
        relay.queue_system_personnel_update = AsyncMock(return_value=True)
        relay._personnel_pending['tama'] = ({'system_name': 'Tama'}, 'old')
        payload = {'state_source': 'system_current_state', 'active_intel': [], 'alerts': [],
                   'hostile_personnel': [], 'monitoring_nodes': []}
        if legacy_unknown:
            payload['active_intel'] = [{'id': 'old', 'source': 'eve-sentry-detector', 'system_name': 'Tama',
                'metadata': {'presence_only': True, 'hostile_icon_count': 2, 'freshness': 'unknown'}}]
            payload['hostile_personnel'] = [{'system_name': 'Tama', 'hostile_count': 2,
                'personnel': [{'character_id': 1, 'name': 'Old'}]}]
        assert await relay.process_bootstrap(payload)
        assert (await relay._load_system_alert_state())[0] == {}
        assert relay._personnel_pending == {}
        relay.deliver_system_transition.assert_not_awaited()
        relay.queue_system_personnel_update.assert_not_awaited()
    await redis.aclose()


def test_node_snapshot_does_not_show_last_known_enemy_count():
    message = format_monitoring_nodes_message([
        {'client_id': 'a', 'system_name': 'Old', 'health_status': 'degraded', 'hostile_count': 8},
        {'client_id': 'b', 'system_name': 'Live', 'health_status': 'online', 'hostile_count': 1},
    ])
    assert 'Old' not in message
    assert '上次' not in message
    assert 'Live' in message
