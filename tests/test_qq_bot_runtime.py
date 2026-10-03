import asyncio
from pathlib import Path

from app.server.qq_bot_runtime import (
    EmbeddedBotBridge,
    EmbeddedEventSource,
    QQBotRuntime,
    QQBotRuntimeConfig,
)


def test_embedded_qq_bot_config_uses_server_scoped_environment():
    config = QQBotRuntimeConfig.from_environment(
        "/srv/eve-sentry/bot/src",
        events_url="http://127.0.0.1:8765/api/v1/events",
        environment={
            "EVE_SENTRY_SERVER_QQ_BOT_APP_ID": "app-id",
            "EVE_SENTRY_SERVER_QQ_BOT_APP_SECRET": "secret",
            "EVE_SENTRY_SERVER_QQ_BOT_DATABASE_URL": "postgresql+asyncpg://bot",
            "EVE_SENTRY_SERVER_QQ_BOT_REDIS_URL": "redis://bot",
            "EVE_SENTRY_SERVER_QQ_BOT_PUBLIC_URL": "https://sentry.example",
            "EVE_SENTRY_SERVER_QQ_BOT_ALERT_MIN_LEVEL": "high",
        },
    )

    assert config.source_root == Path("/srv/eve-sentry/bot/src")
    assert config.events_url.endswith("/api/v1/events")
    assert config.app_id == "app-id"
    assert config.app_secret == "secret"
    assert config.database_url.endswith("/bot")
    assert config.redis_url == "redis://bot"
    assert config.alert_min_level == "high"


def test_embedded_qq_bot_does_not_start_without_credentials(tmp_path):
    runtime = QQBotRuntime(
        QQBotRuntimeConfig(
            source_root=tmp_path,
            events_url="http://127.0.0.1:8765/api/v1/events",
            app_id="",
            app_secret="",
            database_url="",
            redis_url="",
        )
    )

    assert runtime.start() is False
    assert runtime.running is False
    assert runtime.status()["configured"] is False


def test_embedded_event_source_coalesces_store_markers():
    class FakeServer:
        def register_embedded_listener(self, listener):
            self.listener = listener

            def unregister():
                self.listener = None

            return unregister

        def build_embedded_event_snapshot(self, after_seq):
            return {"ready": True, "bootstrap": {}, "events": [], "state_event_seq": 0}

    source = EmbeddedEventSource(FakeServer())
    source.start()
    source.notify()
    source.notify()
    assert source._wakeups.qsize() == 1
    source.close()


def test_embedded_bridge_maps_durable_state_events():
    class Redis:
        async def get(self, _key):
            return None

        async def set(self, key, value):
            self.last = (key, value)

    class Relay:
        def __init__(self):
            self.redis = Redis()
            self.payloads = []

        async def process_alert_event(self, payload):
            self.payloads.append(payload)
            return True

    async def run():
        relay = Relay()
        processed = await EmbeddedBotBridge()._process_event(
            relay,
            {
                "seq": 12,
                "event_key": "alert.entered:s-kswl",
                "event_type": "alert.entered",
                "entity_key": "s-kswl",
                "occurred_at": "2026-10-01T00:00:00+00:00",
                "payload": {"hostile_count": 2, "hostile_personnel": []},
            },
        )
        return processed, relay

    processed, relay = asyncio.run(run())
    assert processed is True
    assert relay.payloads[0]["id"] == "state:12"
    assert relay.payloads[0]["system_name"] == "s-kswl"
    assert relay.payloads[0]["presence_only"] is True


def test_embedded_bridge_uses_latest_bootstrap_without_replaying_old_events():
    class Redis:
        async def get(self, _key):
            return None

        async def set(self, key, value):
            self.last = (key, value)

    class Source:
        _STOP = EmbeddedEventSource._STOP

        def __init__(self):
            self.markers = iter([True, True, self._STOP])

        async def wait(self):
            return next(self.markers)

        def snapshot(self, _after_seq):
            return {
                "ready": True,
                "state_event_seq": 42,
                "bootstrap": {"active_intel": [], "alerts": []},
                "events": [
                    {
                        "seq": 41,
                        "event_type": "alert.entered",
                        "entity_key": "s-kswl",
                        "payload": {"hostile_count": 1},
                    }
                ],
            }

    class Relay:
        def __init__(self):
            self.redis = Redis()
            self.bootstraps = []
            self.events = []

        async def process_bootstrap(self, payload):
            self.bootstraps.append(payload)
            return True

        async def process_alert_event(self, payload):
            self.events.append(payload)
            return True

    async def run():
        relay = Relay()
        await EmbeddedBotBridge().run(Source(), relay)
        return relay

    relay = asyncio.run(run())
    assert len(relay.bootstraps) == 2
    assert relay.events == []
