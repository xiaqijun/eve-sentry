"""Durable QQ transitions must keep moving when Bootstrap is unavailable."""

import asyncio
import importlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.server.qq_bot_runtime import EmbeddedBotBridge, EmbeddedEventSource


@pytest.fixture(autouse=True)
def bot_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "bot" / "src"))
    importlib.import_module("eve_risk.alerts")


class RedisState:
    def __init__(self):
        self.cursor = "state:40"

    async def get(self, _key):
        return self.cursor

    async def exists(self, _key):
        return 1

    async def set(self, _key, value):
        self.cursor = value


class Relay:
    def __init__(self):
        self.redis = RedisState()
        self.deliveries = []
        self.bootstraps = []

    async def process_alert_event(self, payload):
        self.deliveries.append((payload["id"], "alert"))
        return True

    async def process_safe_event(self, payload):
        self.deliveries.append((payload["id"], "safe"))
        return True

    async def process_bootstrap(self, payload):
        self.bootstraps.append(payload)
        return True


def transition(seq, event_type="alert.entered"):
    return {"seq": seq, "event_type": event_type, "entity_key": "s-kswl",
            "payload": {"hostile_count": 0 if event_type == "alert.cleared" else 1}}


class Server:
    def __init__(self, *, blocked=False, ready=True):
        self.rows = []
        self.release = Event()
        self.started = Event()
        self.calls = 0
        self.ready = ready
        if not blocked:
            self.release.set()
        self.store = SimpleNamespace(list_intel_event_page=self.events)

    def register_embedded_listener(self, callback):
        self.callback = callback
        return lambda: None

    def events(self, *, after_seq, since, limit):
        return [row for row in self.rows if row["seq"] > after_seq][:limit]

    def build_embedded_event_snapshot(self, after_seq):
        self.calls += 1
        self.started.set()
        # Deliberately return an old view after release. It must never clear
        # a newer alert that the independent event consumer has acknowledged.
        self.release.wait(10)
        return {"ready": self.ready, "bootstrap": {"active_intel": [], "alerts": []},
                "events": [], "state_event_seq": 40}


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def stop(source, server, task):
    server.release.set()
    source.close()
    try:
        await asyncio.wait_for(task, 1)
    except TimeoutError:
        pass
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A cancelled to_thread(queue.get) still owns a blocking worker on
        # the old implementation. Release it so a red test can terminate.
        try:
            source._wakeups.put_nowait(source._STOP)
        except Exception:
            pass


@pytest.mark.parametrize("ready", [True, False])
def test_durable_alert_and_clear_do_not_wait_for_bootstrap(monkeypatch, ready):
    monkeypatch.setattr(EmbeddedEventSource, "POLL_SECONDS", 0.01, raising=False)

    async def run():
        server, relay = Server(blocked=True, ready=ready), Relay()
        source = EmbeddedEventSource(server)
        source.start()
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(server.started.is_set)
            for seq, kind in [(41, "alert.entered"), (42, "alert.cleared"),
                              (43, "alert.entered")]:
                server.rows.append(transition(seq, kind))
                source.notify()
                await eventually(lambda: relay.redis.cursor == f"state:{seq}")
            assert relay.deliveries == [("state:41", "alert"), ("state:42", "safe"),
                                        ("state:43", "alert")]
            assert server.calls == 1  # Polling must not accumulate blocked workers.
            server.release.set()
            await asyncio.sleep(0.08)
            assert relay.bootstraps == []  # Snapshot 40 is older than ACK 43.
            assert relay.redis.cursor == "state:43"
        finally:
            await stop(source, server, task)

    asyncio.run(run())


def test_durable_events_are_polled_when_change_notification_is_missing(monkeypatch):
    monkeypatch.setattr(EmbeddedEventSource, "POLL_SECONDS", 0.01, raising=False)

    async def run():
        server, relay = Server(), Relay()
        source = EmbeddedEventSource(server)
        source.start()
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: bool(relay.bootstraps))
            server.rows.append(transition(41))
            # No source.notify(): the durable log is the source of truth.
            await eventually(lambda: relay.redis.cursor == "state:41")
            assert relay.deliveries == [("state:41", "alert")]
        finally:
            await stop(source, server, task)

    asyncio.run(run())


def test_durable_replay_drains_multiple_pages_without_duplicate_delivery(monkeypatch):
    monkeypatch.setattr(EmbeddedEventSource, "POLL_SECONDS", 0.01, raising=False)

    async def run():
        server, relay = Server(), Relay()
        server.rows = [transition(seq) for seq in range(41, 146)]
        source = EmbeddedEventSource(server)
        source.start()
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: relay.redis.cursor == "state:145")
            assert [key for key, _ in relay.deliveries] == [
                f"state:{seq}" for seq in range(41, 146)
            ]
            assert relay.bootstraps == [{"active_intel": [], "alerts": []}]
        finally:
            await stop(source, server, task)

    asyncio.run(run())


def test_bootstrap_is_applied_between_its_watermark_and_newer_events(monkeypatch):
    monkeypatch.setattr(EmbeddedEventSource, "POLL_SECONDS", 0.01, raising=False)

    async def run():
        server, relay = Server(), Relay()
        server.rows = [transition(41), transition(42, "alert.cleared")]
        server.build_embedded_event_snapshot = lambda _: {
            "ready": True, "state_event_seq": 41, "bootstrap": {"view": 41},
        }

        async def bootstrap(payload):
            relay.deliveries.append(("bootstrap", relay.redis.cursor))
            return True

        relay.process_bootstrap = bootstrap
        source = EmbeddedEventSource(server)
        source.start()
        task = asyncio.create_task(EmbeddedBotBridge().run(source, relay))
        try:
            await eventually(lambda: relay.redis.cursor == "state:42")
            assert relay.deliveries == [("state:41", "alert"),
                                        ("bootstrap", "state:41"), ("state:42", "safe")]
        finally:
            await stop(source, server, task)

    asyncio.run(run())
