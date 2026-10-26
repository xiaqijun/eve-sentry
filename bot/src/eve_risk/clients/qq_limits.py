"""Shared rolling budgets for QQ proactive group messages."""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections.abc import Callable

from redis.asyncio import Redis
from redis.exceptions import WatchError


class QQProactiveBudget:
    """Reserve attempts atomically across all senders using the same Redis."""

    WINDOW_SECONDS = 61.0  # One second of margin around QQ's minute window.
    GROUP_LIMIT = 20
    BOT_LIMIT = 30  # Also fits QQ's unverified-bot limit.

    def __init__(self, redis: Redis, app_id: str, *, clock: Callable[[], float] = time.time):
        self.redis = redis
        self.clock = clock
        self.prefix = "qq:proactive-budget:" + hashlib.sha256(app_id.encode()).hexdigest()

    def _keys(self, group: str) -> tuple[str, str, str]:
        group_hash = hashlib.sha256(group.encode()).hexdigest()
        group_key = f"{self.prefix}:group:{group_hash}"
        return f"{self.prefix}:bot", group_key, f"{group_key}:cooldown"

    async def reserve(self, group: str) -> float:
        """Return seconds to wait, or reserve one HTTP attempt and return zero."""
        bot_key, group_key, cooldown_key = self._keys(group)
        for _ in range(8):
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(bot_key, group_key, cooldown_key)
                    now = self.clock()
                    cooldown = float(await pipe.get(cooldown_key) or 0)
                    delay = max(0.0, cooldown - now)
                    for key, limit in ((bot_key, self.BOT_LIMIT), (group_key, self.GROUP_LIMIT)):
                        entries = await pipe.zrangebyscore(
                            key, now - self.WINDOW_SECONDS, "+inf", withscores=True,
                        )
                        if len(entries) >= limit:
                            delay = max(delay, entries[0][1] + self.WINDOW_SECONDS - now + 0.01)
                    if delay > 0:
                        return delay
                    member = uuid.uuid4().hex
                    pipe.multi()
                    for key in (bot_key, group_key):
                        pipe.zremrangebyscore(key, "-inf", now - self.WINDOW_SECONDS)
                        pipe.zadd(key, {member: now})
                        pipe.expire(key, math.ceil(self.WINDOW_SECONDS) + 1)
                    await pipe.execute()
                    return 0.0
                except WatchError:
                    continue
        return 1.0  # Contention must not bypass the budget.

    async def defer(self, group: str, seconds: float) -> None:
        """Share QQ's cooldown so Markdown fallbacks cannot send immediately."""
        cooldown_key = self._keys(group)[2]
        while True:
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(cooldown_key)
                    now = self.clock()
                    until = max(float(await pipe.get(cooldown_key) or 0), now + seconds)
                    pipe.multi()
                    pipe.set(cooldown_key, str(until), ex=max(1, math.ceil(until - now)))
                    await pipe.execute()
                    return
                except WatchError:
                    continue
