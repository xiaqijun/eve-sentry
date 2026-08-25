from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
from redis.asyncio import Redis

from eve_risk.clients.qq import QQOpenAPIClient

logger = logging.getLogger(__name__)

ALERT_GROUPS_KEY = "qq:eve-sentry:alert-groups"
ALERT_CURSOR_KEY = "qq:eve-sentry:alert-cursor"
ALERT_DELIVERED_PREFIX = "qq:eve-sentry:alert-delivered"
ACTIVE_INTEL_STATE_KEY = "qq:eve-sentry:active-intel-state"
SYSTEM_ALERT_STATE_KEY = "qq:eve-sentry:system-alert-state"
SYSTEM_ALERT_STATE_READY_KEY = "qq:eve-sentry:system-alert-state-ready"
MONITORING_NODE_SNAPSHOT_STATE_KEY = "qq:eve-sentry:monitoring-node-snapshot-state"
ALERT_DEDUPE_SECONDS = 7 * 24 * 60 * 60
RECONNECT_BACKOFF_SECONDS = (0.2, 1.0, 3.0, 5.0)

_ENABLE_COMMANDS = {"开启预警", "订阅预警", "打开预警"}
_DISABLE_COMMANDS = {"关闭预警", "取消预警", "停止预警"}
_STATUS_COMMANDS = {"预警状态"}
_LEVEL_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "critical": "严重",
}
_LEVEL_RANKS = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_SOURCE_LABELS = {
    "eve-sentry-detector": "OCR 监控",
    "local_ocr": "OCR 监控",
    "ocr": "OCR 监控",
    "intel_channel": "预警频道",
}


def alert_subscription_action(content: str) -> str | None:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"^\s*<@!?\w+>\s*", "", normalized, count=1).strip()
    if normalized in _ENABLE_COMMANDS:
        return "enable"
    if normalized in _DISABLE_COMMANDS:
        return "disable"
    if normalized in _STATUS_COMMANDS:
        return "status"
    return None


def format_active_intel_message(
    item: dict[str, Any],
    transition: str,
    occurred_at: str,
    public_url: str = "",
) -> str:
    entered = transition == "entered"
    title = "### 🔴 敌对进入" if entered else "### 🟢 敌对离开"
    remaining_count = item.get("_remaining_count")
    if isinstance(remaining_count, int) and remaining_count >= 0:
        title = f"{title}｜当前敌对 {remaining_count} 人"
    lines = [
        title,
        f"**位置**｜{_system_label(item)}",
        f"**目标**｜{_target_label(item)}",
    ]
    alliance = _affiliation_label(item, "alliance")
    if alliance:
        lines.append(f"**联盟**｜{alliance}")
    corporation = _affiliation_label(item, "corporation")
    if corporation:
        lines.append(f"**军团**｜{corporation}")
    if entered:
        threat = _threat_label(item)
        if threat:
            lines.append(f"**威胁**｜{threat}")
    lines.append(
        f"**{'进入时间' if entered else '离开时间'}**｜{_format_alert_time(occurred_at)}"
    )
    if not entered:
        duration = _format_duration(str(item.get("first_seen_at") or ""), occurred_at)
        if duration:
            duration_label = "最长停留" if int(item.get("_grouped_count") or 1) > 1 else "停留"
            lines.append(f"**{duration_label}**｜{duration}")
    return "\n".join(lines)


def format_alert_message(alert: dict[str, Any], public_url: str = "") -> str:
    """Backward-compatible formatter for a newly entered active target."""
    occurred_at = str(
        alert.get("first_seen_at") or alert.get("created_at") or datetime.now(UTC).isoformat()
    )
    return format_active_intel_message(alert, "entered", occurred_at, public_url)


def format_system_alert_message(
    system_name: str,
    transition: str,
    hostile_count: int | None = None,
) -> str:
    system_name = str(system_name or "").strip() or "未知星系"
    if transition == "safe":
        return f"✅ {system_name} 清空"
    count_label = (
        f"｜当前敌对 {hostile_count} 人"
        if isinstance(hostile_count, int) and hostile_count >= 0
        else ""
    )
    return f"❗ {system_name} 来敌{count_label}"


def format_system_movement_message(
    from_system: str,
    to_system: str,
    hostile_count: int | None = None,
) -> str:
    from_label = str(from_system or "未知星系").strip() or "未知星系"
    to_label = str(to_system or "未知星系").strip() or "未知星系"
    count_label = (
        f"｜当前敌对 {hostile_count} 人"
        if isinstance(hostile_count, int) and hostile_count >= 0
        else ""
    )
    return f"🔵 敌对移动｜{from_label} → {to_label}{count_label}"


def format_personnel_alert_message(
    state: dict[str, Any],
    occurred_at: str,
) -> str:
    """Format the current personnel snapshot as a compact Markdown table."""
    system_name = _system_label(state)
    hostile_count = state.get("hostile_count")
    count = hostile_count if isinstance(hostile_count, int) else 0
    lines = [
        "### ⚠️ 敌对事件",
        f"**当前敌对**｜{count} 人",
        "| 人员 | 星系 | zKill |",
        "| --- | --- | --- |",
    ]
    personnel = state.get("personnel")
    if not isinstance(personnel, list) or not personnel:
        lines.append("| 暂无已识别人员 | — | — |")
    else:
        for item in personnel:
            if isinstance(item, dict):
                lines.append(_personnel_table_row(item, system_name, occurred_at))
    return "\n".join(lines)


def format_monitoring_node_message(change: dict[str, Any]) -> str:
    change_type = str(change.get("change") or "").strip().casefold()
    system_name = str(change.get("system_name") or "Unknown").strip() or "Unknown"
    if change_type == "online":
        return f"🟢 监控节点上线\n位置｜{system_name}"
    if change_type == "offline":
        return f"⚪ 监控节点下线\n最后位置｜{system_name}"
    from_system = str(change.get("from_system") or "Unknown").strip() or "Unknown"
    to_system = str(change.get("to_system") or system_name).strip() or "Unknown"
    return f"🔵 监控节点移动\n去向｜{from_system} → {to_system}"


def format_monitoring_nodes_message(
    nodes: list[dict[str, Any]],
    changes: list[dict[str, Any]] | None = None,
) -> str:
    """Format the current anonymous online-node list for group delivery.

    ``changes`` remains accepted for event compatibility, but the message is
    intentionally a snapshot only so a group always sees the current list.
    """
    unique: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        client_id = str(node.get("client_id") or "").strip()
        heartbeat_id = str(node.get("heartbeat_client_id") or "").strip()
        source_instance = str(node.get("source_instance") or "").strip()
        identity = client_id or "|".join(
            value for value in (heartbeat_id, source_instance) if value
        )
        if not identity:
            identity = str(node.get("character_name") or "").strip()
        if not identity:
            identity = json.dumps(node, ensure_ascii=False, sort_keys=True)
        if identity:
            unique.setdefault(identity.casefold(), node)

    ordered = sorted(
        unique.values(),
        key=lambda node: (
            str(node.get("system_name") or node.get("system") or "Unknown")
            .strip()
            .casefold(),
            str(node.get("client_id") or node.get("source_instance") or "")
            .strip()
            .casefold(),
        ),
    )
    lines = [f"在线节点｜{len(ordered)}"]
    if not ordered:
        lines.append("暂无在线监控节点")
        return "\n".join(lines)
    for index, node in enumerate(ordered, start=1):
        system_name = str(
            node.get("system_name") or node.get("system") or "Unknown"
        ).strip() or "Unknown"
        lines.append(f"🟢 监控节点 {index}｜{system_name}")
    return "\n".join(lines)


async def iter_sse_events(
    lines: AsyncIterable[str],
) -> AsyncIterator[tuple[str, str, str]]:
    event_name = "message"
    event_id = ""
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, event_id, "\n".join(data_lines)
            event_name = "message"
            event_id = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, event_id, "\n".join(data_lines)


class EveSentryAlertRelay:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        qq: QQOpenAPIClient,
        events_url: str,
        *,
        api_key: str = "",
        min_level: str = "",
        public_url: str = "",
        reconnect_delay_seconds: float = 5.0,
        analysis_enqueue: Callable[[str, dict[str, Any], str, str], Awaitable[bool]]
        | None = None,
    ) -> None:
        self.http = http
        self.redis = redis
        self.qq = qq
        self.events_url = events_url.strip()
        self.api_key = api_key.strip()
        self.min_level = min_level.strip().casefold()
        self.public_url = public_url.strip()
        self.reconnect_delay_seconds = max(0.2, float(reconnect_delay_seconds))
        self.analysis_enqueue = analysis_enqueue
        self._active_alert_ids: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.events_url)

    async def subscribe(self, group_openid: str) -> None:
        await self.redis.sadd(ALERT_GROUPS_KEY, group_openid)

    async def unsubscribe(self, group_openid: str) -> None:
        await self.redis.srem(ALERT_GROUPS_KEY, group_openid)

    async def is_subscribed(self, group_openid: str) -> bool:
        return bool(await self.redis.sismember(ALERT_GROUPS_KEY, group_openid))

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        failure_count = 0
        while True:
            try:
                await self._stream_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = min(
                    RECONNECT_BACKOFF_SECONDS[
                        min(failure_count, len(RECONNECT_BACKOFF_SECONDS) - 1)
                    ],
                    self.reconnect_delay_seconds,
                )
                failure_count += 1
                logger.exception(
                    "EVE Sentry alert stream disconnected; retrying in %.1fs",
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            failure_count = 0
            await asyncio.sleep(0)

    async def deliver(
        self,
        item: dict[str, Any],
        transition: str = "entered",
        occurred_at: str = "",
    ) -> bool:
        active_id = str(item.get("id") or "").strip()
        occurred_at = str(
            occurred_at
            or item.get("first_seen_at")
            or item.get("created_at")
        ).strip()
        if not active_id or not occurred_at:
            logger.warning("Ignored malformed EVE Sentry active intel transition")
            return False

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_active_intel_message(
            item,
            transition,
            occurred_at,
            self.public_url,
        )
        event_id = f"{transition}:{active_id}:{occurred_at}"
        delivered = 0
        failed = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                send_markdown = getattr(self.qq, "send_proactive_markdown", None)
                if send_markdown is None:
                    await self.qq.send_proactive_text(
                        group_openid, _markdown_to_plain_text(message)
                    )
                else:
                    try:
                        await send_markdown(group_openid, message)
                    except Exception:
                        logger.warning(
                            "QQ proactive markdown delivery failed; falling back to text"
                        )
                        await self.qq.send_proactive_text(
                            group_openid, _markdown_to_plain_text(message)
                        )
            except Exception:
                failed += 1
                logger.exception("QQ proactive alert delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        logger.info(
            "EVE Sentry active intel transition processed transition=%s deliveries=%d",
            transition,
            delivered,
        )
        return failed == 0

    async def deliver_system_transition(
        self,
        state: dict[str, Any],
        transition: str,
    ) -> bool:
        system_name = _system_label(state)
        episode_id = str(state.get("episode_id") or "").strip()
        if not episode_id:
            logger.warning("Ignored malformed EVE Sentry system transition")
            return True

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_system_alert_message(
            system_name,
            transition,
            state.get("hostile_count") if transition == "alert" else None,
        )
        event_id = f"system:{transition}:{system_name.casefold()}:{episode_id}"
        delivered = 0
        failed = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                failed += 1
                logger.exception("QQ proactive system alert delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1
            if transition == "alert" and self.analysis_enqueue is not None:
                try:
                    await self.analysis_enqueue(
                        group_openid, state, datetime.now(UTC).isoformat(), event_id
                    )
                except Exception:
                    logger.exception("EVE Sentry hostile analysis enqueue failed")

        logger.info(
            "EVE Sentry system transition processed transition=%s deliveries=%d failures=%d",
            transition,
            delivered,
            failed,
        )
        return failed == 0

    async def deliver_system_movement(
        self,
        from_system: str,
        to_system: str,
        state: dict[str, Any],
        occurred_at: str,
    ) -> bool:
        """Deliver one compact movement event instead of clear plus re-entry."""
        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_system_movement_message(
            from_system,
            to_system,
            state.get("hostile_count"),
        )
        event_id = (
            f"movement:{str(from_system).casefold()}:{str(to_system).casefold()}"
            f":{str(state.get('episode_id') or occurred_at)}"
        )
        failed = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                failed += 1
                logger.exception("QQ proactive movement delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            if self.analysis_enqueue is not None:
                try:
                    await self.analysis_enqueue(group_openid, state, occurred_at, event_id)
                except Exception:
                    logger.exception("EVE Sentry movement analysis enqueue failed")
        logger.info(
            "EVE Sentry movement processed from=%s to=%s failures=%d",
            from_system,
            to_system,
            failed,
        )
        return failed == 0

    async def deliver_system_personnel_update(
        self,
        state: dict[str, Any],
        occurred_at: str,
    ) -> bool:
        """Deliver personnel changes once per system episode and fingerprint."""
        system_name = _system_label(state)
        episode_id = str(state.get("episode_id") or "").strip()
        fingerprint = str(state.get("personnel_fingerprint") or "").strip()
        if not episode_id or not fingerprint:
            logger.warning("Ignored malformed EVE Sentry personnel update")
            return True

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_personnel_alert_message(state, occurred_at)
        event_id = f"personnel:{system_name.casefold()}:{episode_id}:{fingerprint}"
        failed = 0
        delivered = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                send_markdown = getattr(self.qq, "send_proactive_markdown", None)
                if send_markdown is None:
                    await self.qq.send_proactive_text(
                        group_openid, _markdown_to_plain_text(message)
                    )
                else:
                    try:
                        await send_markdown(group_openid, message)
                    except Exception:
                        logger.warning(
                            "QQ proactive personnel markdown delivery failed; falling back to text"
                        )
                        await self.qq.send_proactive_text(
                            group_openid, _markdown_to_plain_text(message)
                        )
            except Exception:
                failed += 1
                logger.exception("QQ proactive personnel delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        logger.info(
            "EVE Sentry personnel update processed system=%s deliveries=%d failures=%d",
            system_name,
            delivered,
            failed,
        )
        return failed == 0

    async def deliver_monitoring_node_change(
        self,
        change: dict[str, Any],
        occurred_at: str,
    ) -> None:
        change_type = str(change.get("change") or "").strip().casefold()
        if change_type not in {"online", "offline", "moved"}:
            logger.warning("Ignored unknown EVE Sentry monitoring node change")
            return
        node_id = str(
            change.get("node_id")
            or change.get("client_id")
            or change.get("source_instance")
            or change.get("character_name")
            or ""
        ).strip()
        if not node_id:
            logger.warning("Ignored monitoring node change without identity")
            return

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        message = format_monitoring_node_message(change)
        event_id = ":".join(
            (
                "node",
                change_type,
                node_id,
                str(change.get("from_system") or ""),
                str(change.get("to_system") or change.get("system_name") or ""),
                occurred_at,
            )
        )
        delivered = 0
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                logger.exception("QQ monitoring node delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1

        logger.info(
            "EVE Sentry monitoring node change processed change=%s deliveries=%d",
            change_type,
            delivered,
        )

    async def deliver_monitoring_node_snapshot(
        self,
        nodes: list[dict[str, Any]],
        occurred_at: str,
        *,
        nodes_version: str = "",
        changes: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Deliver the complete anonymous online-node list once per event."""
        normalized_nodes = [node for node in nodes if isinstance(node, dict)]
        version = str(nodes_version or "").strip()
        if not version:
            version_payload = json.dumps(
                normalized_nodes,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            version = hashlib.sha256(version_payload).hexdigest()[:16]
        change_payload = json.dumps(
            changes or [],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        change_version = hashlib.sha256(change_payload).hexdigest()[:12]
        event_id = f"node-snapshot:{version}:{change_version}:{occurred_at}"

        raw_groups = await self.redis.smembers(ALERT_GROUPS_KEY)
        groups = sorted(_decode(value) for value in raw_groups if _decode(value))
        if not groups:
            return False
        message = format_monitoring_nodes_message(normalized_nodes, changes)
        delivered = 0
        failed = False
        for group_openid in groups:
            delivered_key = _delivered_key(event_id, group_openid)
            if await self.redis.exists(delivered_key):
                continue
            try:
                await self.qq.send_proactive_text(group_openid, message)
            except Exception:
                failed = True
                logger.exception("EVE Sentry monitoring node snapshot delivery failed")
                continue
            await self.redis.set(delivered_key, "1", ex=ALERT_DEDUPE_SECONDS)
            delivered += 1
        if not failed:
            await self.redis.set(
                MONITORING_NODE_SNAPSHOT_STATE_KEY,
                version,
                ex=ALERT_DEDUPE_SECONDS,
            )
        logger.info(
            "EVE Sentry monitoring node snapshot processed nodes=%d deliveries=%d",
            len(normalized_nodes),
            delivered,
        )
        return delivered > 0

    async def process_monitoring_node(self, payload: dict[str, Any]) -> None:
        changes = payload.get("changes")
        occurred_at = str(
            payload.get("generated_at") or datetime.now(UTC).isoformat()
        ).strip()
        nodes = payload.get("nodes")
        if isinstance(nodes, list):
            await self.deliver_monitoring_node_snapshot(
                nodes,
                occurred_at,
                nodes_version=str(payload.get("nodes_version") or ""),
                changes=changes if isinstance(changes, list) else None,
            )
            return
        if not isinstance(changes, list):
            logger.warning("Ignored EVE Sentry monitoring node event without changes")
            return
        for change in changes:
            if isinstance(change, dict):
                await self.deliver_monitoring_node_change(change, occurred_at)

    async def process_bootstrap(self, payload: dict[str, Any]) -> None:
        node_changes = payload.get("monitoring_node_changes")
        monitoring_nodes = payload.get("monitoring_nodes")
        nodes_version = str(payload.get("monitoring_nodes_version") or "").strip()
        if isinstance(node_changes, list) and node_changes and isinstance(
            monitoring_nodes, list
        ):
            await self.process_monitoring_node(
                {
                    "generated_at": payload.get("generated_at"),
                    "changes": node_changes,
                    "nodes": monitoring_nodes,
                    "nodes_version": nodes_version,
                }
            )
        elif isinstance(node_changes, list) and node_changes:
            await self.process_monitoring_node(
                {
                    "generated_at": payload.get("generated_at"),
                    "changes": node_changes,
                }
            )
        elif isinstance(monitoring_nodes, list) and nodes_version:
            last_version = _decode(
                await self.redis.get(MONITORING_NODE_SNAPSHOT_STATE_KEY)
            )
            if last_version != nodes_version:
                await self.deliver_monitoring_node_snapshot(
                    monitoring_nodes,
                    str(payload.get("generated_at") or datetime.now(UTC).isoformat()),
                    nodes_version=nodes_version,
                )
        active_intel = payload.get("active_intel")
        if not isinstance(active_intel, list):
            logger.warning("Ignored EVE Sentry bootstrap without active_intel list")
            return

        generated_at = str(payload.get("generated_at") or datetime.now(UTC).isoformat())
        active_items = {
            active_id: item
            for active_id, item in _active_intel_map(
                active_intel, payload.get("alerts")
            ).items()
            if self._allows_transition(item)
        }
        current = _active_system_state(active_items.values(), generated_at)
        previous, initialized = await self._load_system_alert_state()

        for system_key in current.keys() & previous.keys():
            current[system_key]["episode_id"] = previous[system_key]["episode_id"]

        movement_pairs = _personnel_movement_pairs(previous, current)
        moved_from = {pair["from_system"].casefold() for pair in movement_pairs}
        moved_to = {pair["to_system"].casefold() for pair in movement_pairs}

        transitions_succeeded = True
        if initialized:
            for system_key in sorted(previous.keys() - current.keys()):
                if system_key in moved_from:
                    continue
                transitions_succeeded = (
                    await self.deliver_system_transition(previous[system_key], "safe")
                    and transitions_succeeded
                )
            for system_key in sorted(current.keys() - previous.keys()):
                if system_key in moved_to:
                    continue
                transitions_succeeded = (
                    await self.deliver_system_transition(current[system_key], "alert")
                    and transitions_succeeded
                )
            for pair in movement_pairs:
                transitions_succeeded = (
                    await self.deliver_system_movement(
                        pair["from_system"],
                        pair["to_system"],
                        current[pair["to_key"]],
                        generated_at,
                    )
                    and transitions_succeeded
                )

        if not transitions_succeeded:
            logger.warning("EVE Sentry system state update deferred after delivery failure")
            return

        personnel_updates_succeeded = True
        if initialized:
            previous_personnel = {
                "personnel": [
                    item
                    for previous_state in previous.values()
                    for item in previous_state.get("personnel", [])
                    if isinstance(item, dict)
                ]
            }
            for system_key in sorted(current.keys()):
                current_state = current[system_key]
                previous_state = previous.get(system_key)
                if previous_state is not None and (
                    current_state.get("personnel_fingerprint")
                    == _personnel_fingerprint(previous_state.get("personnel"))
                ):
                    continue
                update_state = _personnel_display_state(
                    current_state,
                    previous_personnel,
                )
                personnel_updates_succeeded = (
                    await self.deliver_system_personnel_update(
                        update_state, generated_at
                    )
                    and personnel_updates_succeeded
                )

        if not personnel_updates_succeeded:
            logger.warning(
                "EVE Sentry personnel state update deferred after delivery failure"
            )
            return

        await self._save_system_alert_state(current)
        active_ids = set(active_items)
        raw_alerts = payload.get("alerts")
        if isinstance(raw_alerts, list):
            for alert in raw_alerts:
                if not isinstance(alert, dict):
                    continue
                active_intel_id = str(alert.get("active_intel_id") or "").strip()
                if active_intel_id in active_ids:
                    alert_id = str(alert.get("id") or "").strip()
                    if alert_id:
                        active_ids.add(alert_id)
        self._active_alert_ids = active_ids
        await self.redis.set(ALERT_CURSOR_KEY, generated_at)
        logger.info(
            "EVE Sentry system alerts synchronized systems=%d hostiles=%d initialized=%s",
            len(current),
            len(active_items),
            initialized,
        )

    async def _load_system_alert_state(
        self,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        ready, raw_items = await asyncio.gather(
            self.redis.exists(SYSTEM_ALERT_STATE_READY_KEY),
            self.redis.hgetall(SYSTEM_ALERT_STATE_KEY),
        )
        result: dict[str, dict[str, Any]] = {}
        for raw_key, raw_payload in raw_items.items():
            system_key = _decode(raw_key)
            try:
                item = json.loads(_decode(raw_payload))
            except json.JSONDecodeError:
                continue
            if system_key and isinstance(item, dict):
                result[system_key] = item
        return result, bool(ready)

    async def _save_system_alert_state(
        self, items: dict[str, dict[str, Any]]
    ) -> None:
        pipeline = self.redis.pipeline()
        pipeline.delete(SYSTEM_ALERT_STATE_KEY)
        if items:
            pipeline.hset(
                SYSTEM_ALERT_STATE_KEY,
                mapping={
                    system_key: json.dumps(
                        item, ensure_ascii=False, separators=(",", ":")
                    )
                    for system_key, item in items.items()
                },
            )
        pipeline.set(SYSTEM_ALERT_STATE_READY_KEY, "1")
        await pipeline.execute()

    async def _load_active_intel_state(self) -> dict[str, dict[str, Any]]:
        raw_items = await self.redis.hgetall(ACTIVE_INTEL_STATE_KEY)
        result: dict[str, dict[str, Any]] = {}
        for raw_id, raw_payload in raw_items.items():
            active_id = _decode(raw_id)
            try:
                item = json.loads(_decode(raw_payload))
            except json.JSONDecodeError:
                continue
            if active_id and isinstance(item, dict):
                result[active_id] = item
        return result

    async def _save_active_intel_state(
        self, items: dict[str, dict[str, Any]]
    ) -> None:
        pipeline = self.redis.pipeline()
        pipeline.delete(ACTIVE_INTEL_STATE_KEY)
        if items:
            pipeline.hset(
                ACTIVE_INTEL_STATE_KEY,
                mapping={
                    active_id: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    for active_id, item in items.items()
                },
            )
        await pipeline.execute()

    def _allows_transition(self, item: dict[str, Any]) -> bool:
        if not self.min_level:
            return True
        level = str(item.get("level") or "").strip().casefold()
        if not level:
            return True
        return _LEVEL_RANKS.get(level, 0) >= _LEVEL_RANKS.get(self.min_level, 0)

    async def _stream_once(self) -> None:
        cursor = _decode(await self.redis.get(ALERT_CURSOR_KEY))
        params = {
            "limit": "50",
            "heartbeat": "15",
            "bootstrap": "1",
            "since": cursor or datetime.now(UTC).isoformat(),
        }
        if self.min_level:
            params["min_level"] = self.min_level
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        async with self.http.stream(
            "GET",
            self.events_url,
            params=params,
            headers={
                "Accept": "text/event-stream",
                **(
                    {"Authorization": f"Bearer {self.api_key}"}
                    if self.api_key
                    else {}
                ),
            },
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for event_name, _event_id, data in iter_sse_events(
                response.aiter_lines()
            ):
                payload = json.loads(data)
                if event_name == "bootstrap" and isinstance(payload, dict):
                    await self.process_bootstrap(payload)
                elif event_name == "alert" and isinstance(payload, dict):
                    await self.process_alert_event(payload)
                elif event_name == "monitoring_node" and isinstance(payload, dict):
                    await self.process_monitoring_node(payload)

    async def process_alert_event(self, payload: dict[str, Any]) -> None:
        """Ignore legacy single-alert events; bootstrap is authoritative.

        The alert SSE event predates system/personnel aggregation and its old
        formatter can race the bootstrap event after reconnects. Delivering it
        here produces a duplicate message with the legacy template, so all
        current notifications are intentionally derived from bootstrap state.
        """
        if payload.get("active") is not False:
            logger.debug(
                "Ignored legacy EVE Sentry alert event; waiting for bootstrap state"
            )


def _active_intel_map(
    raw_items: list[object], raw_alerts: object
) -> dict[str, dict[str, Any]]:
    alerts_by_active_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_alerts, list):
        for raw_alert in raw_alerts:
            if not isinstance(raw_alert, dict):
                continue
            active_id = str(raw_alert.get("active_intel_id") or "").strip()
            if active_id and active_id not in alerts_by_active_id:
                alerts_by_active_id[active_id] = raw_alert

    result: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or raw_item.get("active") is False:
            continue
        active_id = str(raw_item.get("id") or "").strip()
        alert = alerts_by_active_id.get(active_id)
        metadata = raw_item.get("metadata")
        try:
            presence_count = (
                int(metadata.get("hostile_icon_count") or 0)
                if isinstance(metadata, dict)
                else 0
            )
        except (TypeError, ValueError):
            presence_count = 0
        detector_icon_evidence = (
            isinstance(metadata, dict)
            and str(raw_item.get("source") or "").strip().casefold()
            == "eve-sentry-detector"
            and presence_count > 0
        )
        if not active_id or (alert is None and not detector_icon_evidence):
            continue
        item = dict(raw_item)
        if alert is not None:
            for key in ("level", "score", "classification", "names"):
                if item.get(key) in (None, "", []) and alert.get(key) not in (
                    None,
                    "",
                    [],
                ):
                    item[key] = alert[key]
        item_metadata = item.get("metadata")
        alert_metadata = alert.get("metadata") if alert is not None else None
        if isinstance(alert_metadata, dict) or isinstance(item_metadata, dict):
            merged_metadata = dict(alert_metadata) if isinstance(alert_metadata, dict) else {}
            if isinstance(item_metadata, dict):
                for key, value in item_metadata.items():
                    if value not in (None, "", [], {}):
                        merged_metadata[key] = value
            item["metadata"] = merged_metadata
        result[active_id] = item
    return result


def _active_system_state(
    items: Iterable[dict[str, Any]], fallback_episode_id: str
) -> dict[str, dict[str, Any]]:
    systems: dict[str, dict[str, Any]] = {}
    for item in items:
        system_name = _system_label(item)
        system_key = system_name.casefold()
        metadata = _metadata(item)
        is_presence = bool(metadata.get("presence_only"))
        if is_presence:
            try:
                item_count = max(1, int(metadata.get("hostile_icon_count") or 0))
            except (TypeError, ValueError):
                item_count = 1
        else:
            item_count = 1
        state = systems.setdefault(
            system_key,
            {
                "system_name": system_name,
                "hostile_count": 0,
                "episode_id": fallback_episode_id,
                "personnel": [],
                "_detector_counts": {},
            },
        )
        source = str(item.get("source") or "").strip().casefold()
        client_id = str(metadata.get("client_id") or "").strip()
        if source == "eve-sentry-detector" and client_id:
            detector_counts = state["_detector_counts"].setdefault(
                client_id.casefold(), {"presence": 0, "ocr": 0}
            )
            if is_presence:
                detector_counts["presence"] = max(
                    detector_counts["presence"], item_count
                )
            else:
                detector_counts["ocr"] += 1
        else:
            state["hostile_count"] += item_count
        # Red-icon presence has no character identity until optional OCR runs.
        if not is_presence or str(item.get("name") or "").strip():
            state["personnel"].append(_personnel_snapshot(item))
    for state in systems.values():
        state["hostile_count"] += sum(
            max(counts["presence"], counts["ocr"])
            for counts in state.pop("_detector_counts", {}).values()
        )
        personnel = sorted(
            state["personnel"],
            key=lambda item: (
                str(item.get("id") or "").casefold(),
                str(item.get("name") or "").casefold(),
            ),
        )
        state["personnel"] = personnel
        state["personnel_fingerprint"] = _personnel_fingerprint(personnel)
    return systems


def _personnel_fingerprint(value: object) -> str:
    """Fingerprint only personnel fields that should trigger another alert."""
    if not isinstance(value, list):
        return ""
    personnel = [item for item in value if isinstance(item, dict)]
    if not personnel:
        return ""
    visible_identity = sorted(
        [
            {
                "name": str(item.get("name") or "").strip().casefold(),
                "system_name": str(item.get("system_name") or "")
                .strip()
                .casefold(),
            }
            for item in personnel
        ],
        key=lambda item: (item["system_name"], item["name"]),
    )
    return hashlib.sha256(
        json.dumps(
            visible_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]


def _personnel_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(item)
    character_id = (
        item.get("character_id")
        if isinstance(item.get("character_id"), int | str)
        else metadata.get("character_id")
    )
    profile = _character_profile(metadata, character_id)
    personnel_metadata: dict[str, str] = {}
    for key in (
        "alliance_ticker",
        "alliance_name",
        "corporation_ticker",
        "corporation_name",
    ):
        value = metadata.get(key) or profile.get(key)
        if str(value or "").strip():
            personnel_metadata[key] = str(value).strip()
    return {
        "id": str(item.get("id") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "character_id": character_id,
        "system_name": _system_label(item),
        "first_seen_at": str(item.get("first_seen_at") or "").strip(),
        "metadata": personnel_metadata,
        "level": str(item.get("level") or "").strip().casefold(),
        "score": item.get("score") if isinstance(item.get("score"), int | float) else None,
    }


def _personnel_display_state(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """Add a one-message movement label without persisting it in the state."""
    if not previous:
        return current
    previous_by_identity = {
        alias: item
        for item in previous.get("personnel", [])
        if isinstance(item, dict)
        for alias in _personnel_aliases(item)
    }
    displayed = dict(current)
    rows: list[dict[str, Any]] = []
    for item in current.get("personnel", []):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        old = next(
            (
                previous_by_identity[alias]
                for alias in _personnel_aliases(item)
                if alias in previous_by_identity
            ),
            None,
        )
        old_system = str(old.get("system_name") or "").strip() if old else ""
        new_system = str(item.get("system_name") or "").strip()
        if old_system and new_system and old_system.casefold() != new_system.casefold():
            row["system_display"] = f"{old_system} → {new_system}"
        rows.append(row)
    displayed["personnel"] = rows
    return displayed


def _personnel_movement_pairs(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Find complete system-to-system personnel moves in one snapshot."""
    previous_by_identity: dict[str, str] = {}
    for system_key, state in previous.items():
        system_name = str(state.get("system_name") or system_key).strip()
        for item in state.get("personnel", []):
            if isinstance(item, dict):
                for alias in _personnel_aliases(item):
                    previous_by_identity.setdefault(alias, system_name)

    pairs: dict[tuple[str, str], dict[str, str]] = {}
    for state in current.values():
        to_system = str(state.get("system_name") or "").strip()
        to_key = to_system.casefold()
        for item in state.get("personnel", []):
            if not isinstance(item, dict):
                continue
            from_system = next(
                (
                    previous_by_identity[alias]
                    for alias in _personnel_aliases(item)
                    if alias in previous_by_identity
                ),
                "",
            )
            if not from_system or from_system.casefold() == to_key:
                continue
            from_key = from_system.casefold()
            if from_key not in previous:
                continue
            pairs.setdefault(
                (from_key, to_key),
                {
                    "from_system": from_system,
                    "to_system": to_system,
                    "to_key": to_key,
                },
            )
    return list(pairs.values())


def _personnel_identity(item: dict[str, Any]) -> str:
    character_id = str(item.get("character_id") or "").strip()
    if character_id:
        return f"character:{character_id}"
    name = str(item.get("name") or "").strip().casefold()
    if name:
        return f"name:{name}"
    return f"id:{str(item.get('id') or '').strip()}"


def _personnel_aliases(item: dict[str, Any]) -> list[str]:
    """Return stable and OCR-friendly keys for matching a moving person."""
    aliases: list[str] = []
    character_id = str(item.get("character_id") or "").strip()
    if character_id:
        aliases.append(f"character:{character_id}")
    name = str(item.get("name") or "").strip().casefold()
    if name:
        aliases.append(f"name:{name}")
    item_id = str(item.get("id") or "").strip()
    if item_id:
        aliases.append(f"id:{item_id}")
    return aliases or [_personnel_identity(item)]


def _personnel_table_row(
    item: dict[str, Any], fallback_system: str, fallback_time: str
) -> str:
    name = _escape_table_cell(str(item.get("name") or "未知人员").strip() or "未知人员")
    system = str(item.get("system_display") or item.get("system_name") or fallback_system)
    character_id = str(item.get("character_id") or "").strip()
    zkill = (
        f"[🔗](https://zkillboard.com/character/{character_id}/)"
        if character_id.isdigit()
        else "—"
    )
    return "| " + " | ".join(
        (
            name,
            _escape_table_cell(system),
            zkill,
        )
    ) + " |"


def _ticker_label(item: dict[str, Any], kind: str) -> str:
    metadata = _metadata(item)
    return (
        str(metadata.get(f"{kind}_ticker") or metadata.get(f"{kind}_name") or "")
        .strip()
        or "—"
    )


def _character_profile(metadata: dict[str, Any], character_id: object) -> dict[str, Any]:
    profiles = metadata.get("character_profiles")
    if not isinstance(profiles, list):
        return {}
    wanted = str(character_id or "").strip()
    candidates = [profile for profile in profiles if isinstance(profile, dict)]
    for profile in candidates:
        if wanted and str(profile.get("character_id") or "").strip() == wanted:
            return profile
    return candidates[0] if len(candidates) == 1 else {}


def _escape_table_cell(value: str) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ").strip() or "—"


def _transition_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(_transition_group_key(item), []).append(item)
    return [_combine_transition_items(group) for group in grouped.values()]


def _transition_group_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _system_label(item).casefold(),
        str(item.get("source") or "").strip().casefold(),
        str(item.get("source_instance") or "").strip().casefold(),
    )


def _combine_transition_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    if len(items) == 1:
        return items[0]
    ordered = sorted(items, key=lambda item: str(item.get("id") or ""))
    combined = dict(ordered[0])
    active_ids = [str(item.get("id") or "") for item in ordered]
    digest = hashlib.sha256("\0".join(active_ids).encode("utf-8")).hexdigest()[:20]
    combined["id"] = f"batch:{digest}"
    combined["_grouped_count"] = len(ordered)
    names = [str(item.get("name") or "").strip() for item in ordered]
    combined["names"] = [name for name in names if name]
    first_seen_values = [
        str(item.get("first_seen_at") or "").strip()
        for item in ordered
        if str(item.get("first_seen_at") or "").strip()
    ]
    if first_seen_values:
        combined["first_seen_at"] = min(first_seen_values)

    levels = [
        str(item.get("level") or "").strip().casefold()
        for item in ordered
        if str(item.get("level") or "").strip()
    ]
    if levels:
        combined["level"] = max(levels, key=lambda level: _LEVEL_RANKS.get(level, 0))
    scores = [item.get("score") for item in ordered]
    numeric_scores = [score for score in scores if isinstance(score, int | float)]
    if numeric_scores:
        combined["score"] = max(numeric_scores)

    metadata = dict(_metadata(combined))
    for key in (
        "alliance_ticker",
        "alliance_name",
        "corporation_ticker",
        "corporation_name",
    ):
        values = {str(_metadata(item).get(key) or "").strip() for item in ordered}
        if len(values) > 1:
            metadata.pop(key, None)
    combined["metadata"] = metadata
    return combined


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _system_label(item: dict[str, Any]) -> str:
    return str(item.get("system_name") or "未知星系").strip() or "未知星系"


def _target_label(item: dict[str, Any]) -> str:
    raw_names = item.get("names")
    if isinstance(raw_names, list):
        names = [str(name).strip() for name in raw_names if str(name).strip()]
        if names:
            return "、".join(names[:5]) + (f" 等 {len(names)} 人" if len(names) > 5 else "")

    name = str(item.get("name") or "").strip()
    if name:
        return name

    metadata = _metadata(item)
    hostile_count = _positive_int(
        metadata.get("hostile_count")
        or item.get("hostile_count")
        or item.get("target_count")
    )
    raw_text = str(item.get("raw_text") or "").strip()
    if raw_text:
        return raw_text if len(raw_text) <= 80 else raw_text[:77] + "..."
    if hostile_count is not None:
        return f"{hostile_count} 名敌对"
    return str(item.get("display_name") or "未知目标").strip() or "未知目标"


def _affiliation_label(item: dict[str, Any], kind: str) -> str:
    metadata = _metadata(item)
    return _named_affiliation(
        metadata.get(f"{kind}_ticker"), metadata.get(f"{kind}_name")
    )


def _named_affiliation(ticker: object, name: object) -> str:
    ticker_text = str(ticker or "").strip()
    name_text = str(name or "").strip()
    if ticker_text and name_text:
        return f"[{ticker_text}] {name_text}"
    return name_text or (f"[{ticker_text}]" if ticker_text else "")


def _threat_label(item: dict[str, Any]) -> str:
    level = str(item.get("level") or "").strip().casefold()
    label = _LEVEL_LABELS.get(level, level)
    score = item.get("score")
    if label and isinstance(score, int | float):
        return f"{label}（评分 {score}）"
    if label:
        return label
    if isinstance(score, int | float):
        return f"评分 {score}"
    return ""


def _source_label(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "").strip().casefold()
    source_label = _SOURCE_LABELS.get(source, source)
    source_instance = str(item.get("source_instance") or "").strip()
    if source_instance and source_instance.casefold() != source:
        return f"{source_label} · {source_instance}" if source_label else source_instance
    return source_label


def _format_duration(start: str, end: str) -> str:
    started_at = _parse_datetime(start)
    ended_at = _parse_datetime(end)
    if started_at is None or ended_at is None or ended_at < started_at:
        return ""
    seconds = int((ended_at - started_at).total_seconds())
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒" if seconds else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _markdown_to_plain_text(value: str) -> str:
    lines = []
    for raw_line in value.splitlines():
        line = raw_line
        if line.startswith("### "):
            line = line[4:]
        lines.append(line.replace("**", ""))
    return "\n".join(lines)


def _format_alert_time(value: str) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return value or "未知"
    china_time = parsed.astimezone(timezone(timedelta(hours=8)))
    return china_time.strftime("%Y-%m-%d %H:%M:%S")


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value or "")


def _delivered_key(alert_id: str, group_openid: str) -> str:
    group_hash = hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:16]
    return f"{ALERT_DELIVERED_PREFIX}:{alert_id}:{group_hash}"
