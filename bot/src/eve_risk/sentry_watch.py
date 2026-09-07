from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eve_risk.clients.qq import QQOpenAPIClient
from eve_risk.parser import normalize_command_content
from eve_risk.sentry_status import EveSentryStatusClient, SentryStatusError
from eve_risk.storage import SentryWatchRecord

logger = logging.getLogger(__name__)

WATCH_TYPES = {
    "人员": "name",
    "角色": "name",
    "军团": "corporation",
    "联盟": "alliance",
}
WATCH_TYPE_LABELS = {
    "name": "人员",
    "corporation": "军团",
    "alliance": "联盟",
}


@dataclass(frozen=True)
class WatchCommand:
    action: str
    target_type: str = ""
    target_value: str = ""
    interval_seconds: int = 0
    watch_id: str = ""


def parse_watch_command(
    content: str,
    *,
    default_interval: int = 60,
    minimum_interval: int = 30,
) -> WatchCommand | None:
    normalized = _normalize_command(content)
    if normalized == "上线监测":
        return WatchCommand("help")
    if normalized == "上线监测列表":
        return WatchCommand("list")
    if normalized == "取消全部上线监测":
        return WatchCommand("delete_all")
    if normalized.startswith("取消上线监测"):
        argument = normalized[len("取消上线监测"):].strip(" ：:")
        if not argument:
            return WatchCommand("delete_help")
        if re.fullmatch(r"M?[0-9a-fA-F-]{3,36}", argument):
            return WatchCommand("delete", watch_id=argument.removeprefix("M").casefold())
        target_type, target_value = _parse_typed_target(argument)
        if target_type:
            return WatchCommand(
                "delete",
                target_type=target_type,
                target_value=target_value,
            )
        return WatchCommand("delete_help")
    if not normalized.startswith("上线监测"):
        return None
    argument = normalized[len("上线监测"):].strip(" ：:")
    target_type, remainder = _parse_typed_target(argument)
    if not target_type:
        return WatchCommand("help")
    target_value, interval = _split_interval(remainder, default_interval)
    if not target_value:
        return WatchCommand("help")
    if interval < minimum_interval:
        return WatchCommand(
            "interval_too_short",
            target_type=target_type,
            target_value=target_value,
            interval_seconds=interval,
        )
    return WatchCommand(
        "add",
        target_type=target_type,
        target_value=target_value,
        interval_seconds=interval,
    )


def _normalize_command(content: str) -> str:
    return normalize_command_content(content)


def _parse_typed_target(value: str) -> tuple[str, str]:
    normalized = str(value or "").strip()
    for label, target_type in WATCH_TYPES.items():
        if normalized == label:
            return target_type, ""
        if normalized.startswith(f"{label} ") or normalized.startswith(f"{label}：") or normalized.startswith(f"{label}:"):
            return target_type, normalized[len(label):].strip(" ：:")
    return "", normalized


def _split_interval(value: str, default_interval: int) -> tuple[str, int]:
    normalized = str(value or "").strip()
    match = re.search(r"\s+(\d+)\s*(s|秒|m|分|分钟)$", normalized, flags=re.IGNORECASE)
    if not match:
        return normalized, max(1, int(default_interval))
    amount = int(match.group(1))
    unit = match.group(2).casefold()
    seconds = amount * 60 if unit in {"m", "分", "分钟"} else amount
    return normalized[:match.start()].strip(), seconds


def watch_short_id(watch_id: str) -> str:
    return f"M{str(watch_id or '').replace('-', '')[:8].upper()}"


def format_watch_help(minimum_interval: int = 30) -> str:
    return "\n".join(
        (
            "### 🔔 上线监测",
            f"最低监测间隔为 {minimum_interval} 秒。",
            "`上线监测 人员 Alice 30s`",
            "`上线监测 军团 Rat Nation 1m`",
            "`上线监测 联盟 Example Alliance 5m`",
            "`上线监测列表`｜`取消上线监测 M编号`",
        )
    )


def format_watch_list(records: list[SentryWatchRecord]) -> str:
    lines = [
        f"### 🔔 上线监测列表｜{len(records)}",
        "| 编号 | 类型 | 目标 | 间隔 | 状态 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    if not records:
        lines.append("| — | — | 暂无监测任务 | — | — |")
        return "\n".join(lines)
    for record in records:
        lines.append(
            "| " + " | ".join(
                (
                    watch_short_id(record.watch_id),
                    WATCH_TYPE_LABELS.get(record.target_type, record.target_type),
                    _escape_cell(record.target_value),
                    _format_interval(record.interval_seconds),
                    "已发现" if record.present else "监测中",
                )
            ) + " |"
        )
    return "\n".join(lines)


class SentryWatchRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def add(
        self,
        group_openid: str,
        creator_openid: str,
        target_type: str,
        target_value: str,
        interval_seconds: int,
        now: datetime,
    ) -> SentryWatchRecord:
        target_key = target_value.strip().casefold()
        async with self.sessions() as session:
            existing = await session.scalar(
                select(SentryWatchRecord).where(
                    SentryWatchRecord.group_openid == group_openid,
                    SentryWatchRecord.target_type == target_type,
                    SentryWatchRecord.target_key == target_key,
                )
            )
            if existing is None:
                existing = SentryWatchRecord(
                    watch_id=str(uuid.uuid4()),
                    group_openid=group_openid,
                    creator_openid=creator_openid,
                    target_type=target_type,
                    target_value=target_value.strip(),
                    target_key=target_key,
                    interval_seconds=interval_seconds,
                    enabled=True,
                    present=False,
                    missing_count=0,
                    next_check_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
            else:
                existing.creator_openid = creator_openid
                existing.target_value = target_value.strip()
                existing.interval_seconds = interval_seconds
                existing.enabled = True
                existing.next_check_at = now
                existing.updated_at = now
            await session.commit()
            return existing

    async def list_group(self, group_openid: str) -> list[SentryWatchRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(SentryWatchRecord)
                .where(
                    SentryWatchRecord.group_openid == group_openid,
                    SentryWatchRecord.enabled.is_(True),
                )
                .order_by(SentryWatchRecord.created_at, SentryWatchRecord.watch_id)
            )
            return list(rows)

    async def delete_one(
        self,
        group_openid: str,
        *,
        watch_id: str = "",
        target_type: str = "",
        target_value: str = "",
    ) -> SentryWatchRecord | None:
        records = await self.list_group(group_openid)
        normalized_id = watch_id.replace("-", "").casefold()
        target_key = target_value.strip().casefold()
        matched = next(
            (
                record
                for record in records
                if (
                    normalized_id
                    and record.watch_id.replace("-", "").casefold().startswith(normalized_id)
                )
                or (
                    target_type
                    and record.target_type == target_type
                    and record.target_key == target_key
                )
            ),
            None,
        )
        if matched is None:
            return None
        async with self.sessions() as session:
            await session.execute(
                delete(SentryWatchRecord).where(
                    SentryWatchRecord.watch_id == matched.watch_id
                )
            )
            await session.commit()
        return matched

    async def delete_all(self, group_openid: str) -> int:
        async with self.sessions() as session:
            result = await session.execute(
                delete(SentryWatchRecord).where(
                    SentryWatchRecord.group_openid == group_openid
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def due(self, now: datetime, limit: int = 200) -> list[SentryWatchRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(SentryWatchRecord)
                .where(
                    SentryWatchRecord.enabled.is_(True),
                    SentryWatchRecord.next_check_at <= now,
                )
                .order_by(SentryWatchRecord.next_check_at)
                .limit(limit)
            )
            return list(rows)

    async def record_result(
        self,
        watch_id: str,
        *,
        found: bool,
        complete_snapshot: bool,
        now: datetime,
    ) -> bool:
        async with self.sessions() as session:
            record = await session.get(SentryWatchRecord, watch_id)
            if record is None or not record.enabled:
                return False
            notify = False
            if found:
                notify = not record.present
                record.present = True
                record.missing_count = 0
                if notify:
                    record.last_notified_at = now
            elif complete_snapshot:
                record.missing_count += 1
                if record.missing_count >= 2:
                    record.present = False
                    record.missing_count = 0
            record.last_checked_at = now
            record.next_check_at = now + timedelta(seconds=record.interval_seconds)
            record.updated_at = now
            await session.commit()
            return notify

    async def retry_notification(self, watch_id: str, now: datetime) -> None:
        async with self.sessions() as session:
            record = await session.get(SentryWatchRecord, watch_id)
            if record is None:
                return
            record.present = False
            record.next_check_at = now + timedelta(seconds=record.interval_seconds)
            record.updated_at = now
            await session.commit()


class SentryWatchMonitor:
    def __init__(
        self,
        repository: SentryWatchRepository,
        sentry: EveSentryStatusClient,
        qq: QQOpenAPIClient,
        redis: Redis,
        *,
        poll_seconds: float = 5.0,
        query_timeout_seconds: float = 15.0,
        snapshot_reuse_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.sentry = sentry
        self.qq = qq
        self.redis = redis
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.query_timeout_seconds = max(1.0, float(query_timeout_seconds))
        self.snapshot_reuse_seconds = max(0, int(snapshot_reuse_seconds))
        self._cached_payload: dict[str, Any] | None = None
        self._cached_until = datetime.min.replace(tzinfo=UTC)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EVE Sentry watch cycle failed")
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> None:
        now = datetime.now(UTC)
        due = await self.repository.due(now)
        if not due or not self.sentry.enabled:
            return
        if self._cached_payload is not None and now < self._cached_until:
            await self._process_snapshot(due, self._cached_payload, now)
            return
        token = uuid.uuid4().hex
        locked = await self.redis.set(
            "sentry-watch:scan-lock",
            token,
            ex=max(30, int(self.query_timeout_seconds) + 15),
            nx=True,
        )
        if not locked:
            return
        try:
            try:
                created = await self.sentry.create_ocr_query({"mode": "all_nodes"})
                payload = await self.sentry.wait_ocr_query_payload(
                    created,
                    timeout_seconds=self.query_timeout_seconds,
                )
            except SentryStatusError:
                logger.warning("EVE Sentry watch scan did not return a usable snapshot")
                return

            expected = int(payload.get("expected_clients") or 0)
            received = int(payload.get("received_clients") or 0)
            complete = expected > 0 and received >= expected
            if complete and self.snapshot_reuse_seconds:
                self._cached_payload = payload
                self._cached_until = datetime.now(UTC) + timedelta(
                    seconds=self.snapshot_reuse_seconds
                )
            await self._process_snapshot(due, payload, now)
        finally:
            if await self.redis.get("sentry-watch:scan-lock") in {token, token.encode()}:
                await self.redis.delete("sentry-watch:scan-lock")

    async def _process_snapshot(
        self,
        due: list[SentryWatchRecord],
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        expected = int(payload.get("expected_clients") or 0)
        received = int(payload.get("received_clients") or 0)
        complete = expected > 0 and received >= expected
        for watch in due:
            matches = _watch_matches(payload, watch)
            should_notify = await self.repository.record_result(
                watch.watch_id,
                found=bool(matches),
                complete_snapshot=complete,
                now=now,
            )
            if not should_notify:
                continue
            try:
                await self.qq.send_proactive_markdown(
                    watch.group_openid,
                    format_watch_notification(watch, matches),
                )
            except Exception:
                logger.exception("EVE Sentry watch notification failed")
                await self.repository.retry_notification(watch.watch_id, now)


def _watch_matches(
    payload: dict[str, Any],
    watch: SentryWatchRecord,
) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    target = watch.target_key
    results = payload.get("results")
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        system_name = str(result.get("system_name") or "未知星系").strip()
        recognized = result.get("recognized")
        recognized_items = [item for item in recognized if isinstance(item, dict)] if isinstance(recognized, list) else []
        by_name = {
            str(item.get("name") or "").strip().casefold(): item
            for item in recognized_items
            if str(item.get("name") or "").strip()
        }
        for raw_name in result.get("names", []):
            name = str(raw_name or "").strip()
            if not name:
                continue
            item = by_name.get(name.casefold(), {})
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            candidate = name
            if watch.target_type == "corporation":
                candidates = {
                    str(metadata.get("corporation_name") or "").strip().casefold(),
                    str(metadata.get("corporation_ticker") or "").strip().casefold(),
                }
            elif watch.target_type == "alliance":
                candidates = {
                    str(metadata.get("alliance_name") or "").strip().casefold(),
                    str(metadata.get("alliance_ticker") or "").strip().casefold(),
                }
            else:
                candidates = {candidate.strip().casefold()}
            if target not in candidates:
                continue
            matches.append(
                {
                    "name": name,
                    "system_name": system_name,
                    "corporation": str(metadata.get("corporation_name") or "—"),
                    "alliance": str(metadata.get("alliance_name") or "—"),
                }
            )
    return matches


def format_watch_notification(
    watch: SentryWatchRecord,
    matches: list[dict[str, str]],
) -> str:
    lines = [
        "### 🔔 上线监测发现目标",
        f"**目标**｜{WATCH_TYPE_LABELS.get(watch.target_type, watch.target_type)}：{watch.target_value}",
        "| 人员 | 星系 | 军团 | 联盟 |",
        "| --- | --- | --- | --- |",
    ]
    for item in matches[:30]:
        lines.append(
            "| " + " | ".join(
                _escape_cell(item.get(key) or "—")
                for key in ("name", "system_name", "corporation", "alliance")
            ) + " |"
        )
    return "\n".join(lines)


def _format_interval(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"


def _escape_cell(value: object) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ").strip() or "—"
