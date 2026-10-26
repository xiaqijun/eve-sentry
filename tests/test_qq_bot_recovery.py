"""Embedded QQ delivery recovers without replaying obsolete warning bursts."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.server.qq_bot_runtime import EmbeddedBotBridge, EmbeddedEventSource


@pytest.fixture(autouse=True)
def bot_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "bot" / "src"))
    pytest.importorskip("redis")
    monkeypatch.setattr(EmbeddedBotBridge, "RETRY_SECONDS", 0.01)


class RedisState:
    def __init__(self):
        self.cursor = "state:40"
        self.saved = []

    async def get(self, _key):
        return self.cursor

    async def exists(self, _key):
        return 1

    async def set(self, _key, value):
        self.cursor = value
        self.saved.append(int(value.split(":")[1]))


class Relay:
    def __init__(self):
        self.redis = RedisState()
        self.deliveries = []
        self.bootstraps = []
        self.qq = SimpleNamespace(rate_limit_generation=0, retry_after_seconds=0.0)

    async def process_alert_event(self, payload):
        self.deliveries.append(payload["id"])
        return True

    async def process_safe_event(self, payload):
        self.deliveries.append(payload["id"])
        return True

    async def process_bootstrap(self, payload):
        self.bootstraps.append(payload)
        return True


class Source:
    def __init__(self):
        self.rows = []
        self._closed = False
        self.snapshot_calls = 0
        self.view = {"ready": False}
        self.release = Event()
        self.release.set()

    async def wait(self):
        await asyncio.sleep(0.005)
        return EmbeddedEventSource._STOP if self._closed else True

    def events(self, after_seq):
        return [row for row in self.rows if row["seq"] > after_seq][:100]

    def snapshot(self, _after_seq):
        self.snapshot_calls += 1
        self.release.wait(5)
        return self.view

    def close(self):
        self._closed = True
        self.release.set()


def transition(seq, *, old=False):
    created = datetime.now(UTC) - timedelta(minutes=10 if old else 0)
    return {"seq": seq, "created_at": created.isoformat(),
            "occurred_at": "2000-01-01T00:00:00Z",  # Skewed client clock.
            "event_type": "alert.entered", "entity_key": "Tama",
            "payload": {"system_name": "Tama", "hostile_count": 1}}


def snapshot(watermark):
    return {"ready": True, "state_event_seq": watermark,
            "bootstrap": {"generated_at": datetime.now(UTC).isoformat(),
                          "active_intel": [], "alerts": []}}


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def stop(source, task):
    source.close()
    try:
        await asyncio.wait_for(task, 0.5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_failed_delivery_retries_from_persisted_ack_and_keeps_one_blocked_snapshot():
    async def run():
        source, relay = Source(), Relay()
        source.release.clear()
        source.rows = [transition(41), transition(42)]
        attempts = []

        async def deliver(payload):
            attempts.append(payload["id"])
            if len(attempts) == 2:
                assert relay.redis.cursor == "state:41"
                return False
            relay.deliveries.append(payload["id"])
            return True

        relay.process_alert_event = deliver
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: relay.redis.cursor == "state:42")
            assert not task.done()
            assert attempts == ["state:41", "state:42", "state:42"]
            assert relay.deliveries == ["state:41", "state:42"]
            assert source.snapshot_calls == 1
        finally:
            await stop(source, task)

    asyncio.run(run())


def test_old_backlog_waits_for_fresh_snapshot_and_acknowledges_only_after_reconciliation():
    async def run():
        source, relay = Source(), Relay()
        source.rows = [transition(i, old=True) for i in range(41, 274)]
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: source.snapshot_calls >= 3)
            assert relay.redis.cursor == "state:40" and relay.deliveries == []
            stale = snapshot(273)
            stale["bootstrap"]["generated_at"] = "2000-01-01T00:00:00Z"
            source.view = stale
            await asyncio.sleep(0.03)
            assert relay.redis.cursor == "state:40" and relay.bootstraps == []
            failures = []

            async def reconcile(payload):
                failures.append(1)
                assert relay.redis.cursor == "state:40"
                if len(failures) == 1:
                    return False
                relay.bootstraps.append(payload)
                return True

            relay.process_bootstrap = reconcile
            source.view = snapshot(273)
            await eventually(lambda: relay.redis.cursor == "state:273")
            assert len(failures) == 2 and relay.deliveries == []
            assert len(relay.bootstraps) == 1
            source.view = {"ready": False}
            source.rows.append(transition(274))
            await eventually(lambda: relay.redis.cursor == "state:274")
            assert relay.deliveries == ["state:274"]
        finally:
            await stop(source, task)

    asyncio.run(run())


def test_expiry_after_partial_page_does_not_rewind_a_successful_ack():
    async def run():
        source, relay = Source(), Relay()
        source.rows = [transition(41), transition(42, old=True)]
        source.view = snapshot(40)
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: relay.redis.cursor == "state:41")
            await asyncio.sleep(0.06)
            assert relay.redis.cursor == "state:41"
            assert relay.redis.saved == sorted(relay.redis.saved)
            assert relay.deliveries == ["state:41"]
            source.view = snapshot(42)
            await eventually(lambda: relay.redis.cursor == "state:42")
        finally:
            await stop(source, task)

    asyncio.run(run())


def test_close_interrupts_a_long_rate_limit_backoff():
    async def run():
        source, relay = Source(), Relay()
        relay.qq.rate_limit_generation = 1
        relay.qq.retry_after_seconds = 3600
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        await asyncio.sleep(0.02)
        await stop(source, task)
        assert task.done() and not task.cancelled()
        assert relay.redis.cursor == "state:40"

    asyncio.run(run())


@pytest.mark.parametrize("decode_responses", [False, True])
def test_real_qq_rate_error_recovers_through_latest_state_then_delivers_a_new_alert(decode_responses):
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    import httpx

    from eve_risk.alerts import (
        ALERT_EVENT_ID_KEY, ALERT_GROUPS_KEY, SYSTEM_ALERT_STATE_KEY,
        SYSTEM_ALERT_STATE_READY_KEY, EveSentryAlertRelay,
    )
    from eve_risk.clients.qq import QQOpenAPIClient

    async def run():
        redis = fakeredis.FakeRedis(decode_responses=decode_responses)
        await redis.set("qq:access-token:app", "test-token")
        await redis.set(ALERT_EVENT_ID_KEY, "state:40")
        await redis.set(SYSTEM_ALERT_STATE_READY_KEY, "1")
        await redis.sadd(ALERT_GROUPS_KEY, "test-group")
        await redis.hset(SYSTEM_ALERT_STATE_KEY, "tama", json.dumps({
            "system_name": "Tama", "hostile_count": 1, "episode_id": "test-wave",
            "personnel": [], "personnel_revision": 0,
        }))
        source = Source()
        clear = transition(41)
        clear.update(event_type="alert.cleared", payload={"system_name": "Tama", "hostile_count": 0})
        source.rows = [clear]
        requests = []

        def send(request):
            requests.append(request)
            if len(requests) == 1:
                source.view = snapshot(41)
                return httpx.Response(400, headers={"Retry-After": "0.01"},
                                      json={"err_code": 40034100, "trace_id": "test-trace"})
            return httpx.Response(200, json={"id": f"test-message-{len(requests)}"})

        task = None
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
                qq = QQOpenAPIClient(http, redis, "app", "secret", "https://qq.test/token", "https://qq.test")
                relay = EveSentryAlertRelay(http, redis, qq, "https://sentry.test/events")
                task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
                async with asyncio.timeout(2):
                    while await redis.get(ALERT_EVENT_ID_KEY) != ("state:41" if decode_responses else b"state:41"):
                        await asyncio.sleep(0.005)
                assert not task.done() and len(requests) == 2
                assert "清空" in json.loads(requests[-1].content)["content"]
                source.view = {"ready": False}
                source.rows.append(transition(42))
                async with asyncio.timeout(2):
                    while await redis.get(ALERT_EVENT_ID_KEY) != ("state:42" if decode_responses else b"state:42"):
                        await asyncio.sleep(0.005)
                assert not task.done() and len(requests) == 3
                assert "来敌" in json.loads(requests[-1].content)["content"]
        finally:
            if task is not None:
                await stop(source, task)
            await redis.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("value,expected", [(b"state:41", 41), ("state:41", 41),
                                           (None, 0), (b"state:bad", 0), (b"\xff", 0)])
def test_persisted_cursor_accepts_default_redis_byte_responses(value, expected):
    assert EmbeddedBotBridge._read_state_sequence(value) == expected


def test_partial_delivery_recovery_reads_a_byte_cursor_without_reinitializing():
    async def run():
        source, relay = Source(), Relay()
        source.rows = [transition(41), transition(42)]
        original_get = relay.redis.get

        async def byte_get(key):
            return (await original_get(key)).encode()

        relay.redis.get = byte_get
        attempts = []

        async def deliver(payload):
            attempts.append(payload["id"])
            return len(attempts) != 2

        relay.process_alert_event = deliver
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: relay.redis.cursor == "state:42")
            assert not task.done()
            assert attempts == ["state:41", "state:42", "state:42"]
            assert relay.redis.saved == [41, 42]
        finally:
            await stop(source, task)

    asyncio.run(run())
