"""Run the QQ bot inside the EVE Sentry server process.

The bot remains in ``bot/src`` so its command handlers and delivery logic are
not duplicated.  This adapter owns only the lifecycle boundary: it prepares
the bot settings, starts botpy on a dedicated asyncio thread, and closes the
client during server shutdown.  The QQ client never runs on the HTTP worker
threads, so a slow QQ API cannot block ingestion, SSE, or readiness probes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EmbeddedEventSource:
    """Coalesced same-process wake-ups backed by durable server state."""

    _STOP = object()

    def __init__(self, server: Any) -> None:
        self.server = server
        self._wakeups: queue.Queue[object] = queue.Queue(maxsize=1)
        self._unregister: Any | None = None
        self._closed = False

    def start(self) -> None:
        if self._unregister is not None:
            return
        self._unregister = self.server.register_embedded_listener(self.notify)
        self.notify()

    def notify(self) -> None:
        if self._closed:
            return
        try:
            self._wakeups.put_nowait(True)
        except queue.Full:
            # A marker already means “read the latest durable state”; another
            # marker cannot add information.
            pass

    async def wait(self) -> object:
        return await asyncio.to_thread(self._wakeups.get)

    def snapshot(self, after_seq: int) -> dict[str, Any]:
        return self.server.build_embedded_event_snapshot(after_seq)

    def close(self) -> None:
        self._closed = True
        unregister = self._unregister
        self._unregister = None
        if callable(unregister):
            unregister()
        try:
            self._wakeups.put_nowait(self._STOP)
        except queue.Full:
            pass


class EmbeddedBotBridge:
    """Consume durable alert transitions without loopback HTTP/SSE."""

    async def run(self, source: EmbeddedEventSource, relay: Any) -> None:
        from eve_risk.alerts import ALERT_EVENT_ID_KEY, SYSTEM_ALERT_STATE_READY_KEY

        cursor = self._read_state_sequence(
            await relay.redis.get(ALERT_EVENT_ID_KEY)
        )
        state_ready = (
            cursor > 0
            and await self._redis_key_exists(relay, SYSTEM_ALERT_STATE_READY_KEY)
        )
        initialized = False
        while True:
            marker = await source.wait()
            if marker is EmbeddedEventSource._STOP:
                return
            snapshot = await asyncio.to_thread(source.snapshot, cursor)
            if not snapshot.get("ready"):
                continue
            bootstrap = snapshot.get("bootstrap")
            if not isinstance(bootstrap, dict):
                continue
            state_seq = max(0, int(snapshot.get("state_event_seq") or 0))
            if not initialized and not state_ready:
                if not await relay.process_bootstrap(bootstrap):
                    raise RuntimeError("embedded bot bootstrap delivery failed")
                initialized = True
                state_ready = True
                cursor = max(cursor, state_seq)
                await self._save_cursor(relay, ALERT_EVENT_ID_KEY, cursor)
                continue

            initialized = True
            state_ready = True
            cursor, snapshot = await self._replay_to_watermark(
                source,
                relay,
                cursor,
                snapshot,
            )
            latest_bootstrap = snapshot.get("bootstrap")
            if not isinstance(latest_bootstrap, dict):
                latest_bootstrap = bootstrap
            if not await relay.process_bootstrap(latest_bootstrap):
                raise RuntimeError("embedded bot bootstrap delivery failed")
            cursor = max(
                cursor,
                int(snapshot.get("state_event_seq") or 0),
                state_seq,
            )
            await self._save_cursor(relay, ALERT_EVENT_ID_KEY, cursor)

    async def _replay_to_watermark(
        self,
        source: EmbeddedEventSource,
        relay: Any,
        cursor: int,
        snapshot: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Drain every durable event up to the newest same-process watermark.

        Store notifications are intentionally coalesced, so a single wake-up
        can cover an enter, clear, and re-enter sequence.  The event page is
        the lossless part of that contract; the final bootstrap only repairs
        the state after all acknowledged events have been applied.
        """
        from eve_risk.alerts import ALERT_EVENT_ID_KEY

        while True:
            page_events = snapshot.get("events")
            if not isinstance(page_events, list):
                page_events = []
            progressed = False
            for event in page_events:
                if not isinstance(event, dict):
                    continue
                event_seq = max(0, int(event.get("seq") or 0))
                if event_seq <= cursor:
                    continue
                processed = await self._process_event(relay, event)
                if not processed:
                    raise RuntimeError(
                        "embedded bot event processing failed; retry from cursor"
                    )
                cursor = event_seq
                progressed = True
                await self._save_cursor(relay, ALERT_EVENT_ID_KEY, cursor)

            state_seq = max(0, int(snapshot.get("state_event_seq") or 0))
            if not progressed:
                if state_seq > cursor:
                    # The durable page can legitimately contain only events
                    # that are outside the current event projection. Advance
                    # to the observed state watermark before reconciliation.
                    cursor = state_seq
                    await self._save_cursor(relay, ALERT_EVENT_ID_KEY, cursor)
                return cursor, snapshot

            snapshot = await asyncio.to_thread(source.snapshot, cursor)
            if not snapshot.get("ready"):
                return cursor, snapshot

    @staticmethod
    async def _redis_key_exists(relay: Any, key: str) -> bool:
        exists = getattr(relay.redis, "exists", None)
        if not callable(exists):
            return False
        return bool(await exists(key))

    async def _process_event(self, relay: Any, event: dict[str, Any]) -> bool:
        event_type = str(event.get("event_type") or "").strip()
        payload = dict(event.get("payload") or {})
        system_name = str(
            payload.get("system_name") or event.get("entity_key") or ""
        ).strip()
        if not system_name:
            return True
        event_seq = max(0, int(event.get("seq") or 0))
        payload.update(
            {
                "id": f"state:{event_seq}",
                "event_key": event.get("event_key"),
                "event_type": event_type,
                "system_name": system_name,
                "system": system_name,
                "created_at": event.get("occurred_at") or "",
                "active": event_type != "alert.cleared",
                "message": (
                    f"✅ {system_name} 清空"
                    if event_type == "alert.cleared"
                    else f"❗ {system_name} 来敌"
                ),
            }
        )
        try:
            payload["hostile_count"] = max(0, int(payload.get("hostile_count") or 0))
        except (TypeError, ValueError):
            payload["hostile_count"] = 0
        payload["presence_only"] = not bool(payload.get("hostile_personnel"))
        if event_type == "alert.cleared":
            payload["active"] = False
            return await relay.process_safe_event(payload)
        if event_type in {"alert.entered", "alert.updated"}:
            return await relay.process_alert_event(payload)
        return True

    @staticmethod
    def _read_state_sequence(value: object) -> int:
        text = str(value or "").strip()
        if not text.startswith("state:"):
            return 0
        try:
            return max(0, int(text.split(":", 1)[1]))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    async def _save_cursor(relay: Any, key: str, sequence: int) -> None:
        await relay.redis.set(key, f"state:{max(0, int(sequence))}")


@dataclass(frozen=True)
class QQBotRuntimeConfig:
    """Configuration needed to embed the existing bot package."""

    source_root: Path
    events_url: str
    app_id: str
    app_secret: str
    database_url: str
    redis_url: str
    public_url: str = ""
    alert_min_level: str = ""

    @classmethod
    def from_environment(
        cls,
        source_root: str | Path,
        *,
        events_url: str,
        environment: dict[str, str] | None = None,
    ) -> "QQBotRuntimeConfig":
        values = environment if environment is not None else os.environ

        def value(name: str) -> str:
            return str(values.get(name, "") or "").strip()

        return cls(
            source_root=Path(source_root).expanduser(),
            events_url=str(events_url or "").strip(),
            app_id=value("EVE_SENTRY_SERVER_QQ_BOT_APP_ID"),
            app_secret=value("EVE_SENTRY_SERVER_QQ_BOT_APP_SECRET"),
            database_url=value("EVE_SENTRY_SERVER_QQ_BOT_DATABASE_URL"),
            redis_url=value("EVE_SENTRY_SERVER_QQ_BOT_REDIS_URL"),
            public_url=value("EVE_SENTRY_SERVER_QQ_BOT_PUBLIC_URL"),
            alert_min_level=value("EVE_SENTRY_SERVER_QQ_BOT_ALERT_MIN_LEVEL"),
        )


class QQBotRuntime:
    """Lifecycle wrapper for the botpy client used by the warning server."""

    def __init__(
        self,
        config: QQBotRuntimeConfig,
        *,
        event_source: EmbeddedEventSource | None = None,
    ) -> None:
        self.config = config
        self.event_source = event_source
        self._thread: threading.Thread | None = None
        self._client: object | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.config.app_id and self.config.app_secret)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> bool:
        """Start the embedded bot without making server startup depend on QQ."""
        if not self.enabled:
            logger.warning(
                "Embedded QQ bot is enabled but app credentials are missing; "
                "warning server will continue without QQ delivery"
            )
            return False
        source_root = self.config.source_root.resolve()
        package_root = source_root / "eve_risk"
        if not package_root.is_dir():
            self._last_error = f"bot source directory not found: {package_root}"
            logger.error(self._last_error)
            return False
        if self.running:
            return True
        if self.event_source is not None:
            self.event_source.start()
        self._thread = threading.Thread(
            target=self._run,
            name="eve-sentry-qq-bot",
            daemon=True,
        )
        self._thread.start()
        # The thread may fail while importing optional bot dependencies.  Wait
        # briefly so the log reports that failure during startup rather than
        # several minutes later, but never hold the HTTP server hostage.
        self._started.wait(timeout=2.0)
        return self.running

    def stop(self, timeout: float = 15.0) -> None:
        """Close the bot client and join its asyncio thread."""
        if self.event_source is not None:
            self.event_source.close()
        client = self._client
        loop = self._client_loop
        if client is not None and loop is not None and not loop.is_closed():
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    future = asyncio.run_coroutine_threadsafe(close(), loop)
                    future.result(timeout=max(1.0, float(timeout)))
                except Exception:
                    logger.warning("Embedded QQ bot close failed", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, float(timeout)))
            if thread.is_alive():
                logger.error("Embedded QQ bot did not stop within %.1fs", timeout)
        self._thread = None

    def status(self) -> dict[str, object]:
        """Return a redacted status payload suitable for readiness diagnostics."""
        return {
            "enabled": self.enabled,
            "running": self.running,
            "configured": bool(self.config.app_id and self.config.app_secret),
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._client_loop = loop
        try:
            source = str(self.config.source_root.resolve())
            if source not in sys.path:
                sys.path.insert(0, source)
            self._prepare_environment()

            import botpy  # type: ignore[import-not-found]
            from eve_risk.bot import RiskBotClient

            intents = botpy.Intents(public_messages=True, interaction=True)
            client = RiskBotClient(intents=intents, bot_log=False)
            if self.event_source is not None:
                client.embedded_event_bridge = EmbeddedBotBridge()
                client.embedded_event_bridge_source = self.event_source
            self._client = client
            self._started.set()
            logger.info("Embedded QQ bot starting")
            client.run(appid=self.config.app_id, secret=self.config.app_secret)
        except Exception as exc:
            self._last_error = str(exc)
            self._started.set()
            logger.exception("Embedded QQ bot stopped unexpectedly")
        finally:
            self._client = None
            self._client_loop = None
            self._stopped.set()
            try:
                loop.close()
            except Exception:
                logger.debug("Embedded QQ bot event loop close failed", exc_info=True)
            asyncio.set_event_loop(None)

    def _prepare_environment(self) -> None:
        """Map server-scoped settings to the bot's existing settings names."""
        mappings = {
            "QQ_APP_ID": self.config.app_id,
            "QQ_APP_SECRET": self.config.app_secret,
            "DATABASE_URL": self.config.database_url,
            "REDIS_URL": self.config.redis_url,
            "EVE_SENTRY_EVENTS_URL": self.config.events_url,
            "EVE_SENTRY_PUBLIC_URL": self.config.public_url,
            "EVE_SENTRY_ALERT_MIN_LEVEL": self.config.alert_min_level,
        }
        for name, value in mappings.items():
            if value:
                os.environ[name] = value
        # An embedded bot is same-host by construction.  Never reuse a stale
        # revoked Bearer key from an old standalone bot environment.
        os.environ["EVE_SENTRY_API_KEY"] = ""
        os.environ["EVE_SENTRY_EMBEDDED_BOT"] = "1"
        os.environ["EVE_SENTRY_EMBEDDED_DIRECT"] = (
            "1" if self.event_source is not None else "0"
        )
