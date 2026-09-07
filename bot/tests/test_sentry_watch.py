from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from eve_risk.sentry_watch import (
    SentryWatchMonitor,
    _watch_matches,
    format_watch_list,
    parse_watch_command,
)


def _watch(target_type: str = "name", target_value: str = "Alice") -> SimpleNamespace:
    return SimpleNamespace(
        watch_id="12345678-1234-1234-1234-123456789abc",
        group_openid="group-1",
        creator_openid="member-1",
        target_type=target_type,
        target_value=target_value,
        target_key=target_value.casefold(),
        interval_seconds=30,
        enabled=True,
        present=False,
        created_at=datetime.now(UTC),
    )


def test_parse_watch_commands_and_minimum_interval() -> None:
    command = parse_watch_command("上线监测 人员 Alice 30s")
    assert command is not None
    assert command.action == "add"
    assert command.target_type == "name"
    assert command.target_value == "Alice"
    assert command.interval_seconds == 30

    command = parse_watch_command("@哨兵/上线监测 军团 Rat Nation 2m")
    assert command is not None
    assert command.target_type == "corporation"
    assert command.target_value == "Rat Nation"
    assert command.interval_seconds == 120

    too_fast = parse_watch_command("上线监测 联盟 Example Alliance 20s")
    assert too_fast is not None
    assert too_fast.action == "interval_too_short"

    assert parse_watch_command("上线监测列表").action == "list"
    assert parse_watch_command("取消上线监测 M12345678").watch_id == "12345678"
    assert parse_watch_command("取消上线监测 M001").watch_id == "001"
    assert parse_watch_command("取消上线监测 人员 Alice").target_value == "Alice"
    assert parse_watch_command("取消全部上线监测").action == "delete_all"


def test_watch_matching_uses_current_ocr_names_and_affiliations() -> None:
    payload = {
        "results": [
            {
                "system_name": "S-KSWL",
                "names": ["Alice", "Bob"],
                "recognized": [
                    {
                        "name": "Alice",
                        "metadata": {
                            "corporation_name": "Rat Nation",
                            "alliance_name": "Example Alliance",
                        },
                    }
                ],
            }
        ]
    }

    assert [item["name"] for item in _watch_matches(payload, _watch())] == ["Alice"]
    assert [
        item["name"]
        for item in _watch_matches(payload, _watch("corporation", "Rat Nation"))
    ] == ["Alice"]
    assert [
        item["name"]
        for item in _watch_matches(payload, _watch("alliance", "Example Alliance"))
    ] == ["Alice"]


def test_format_watch_list_includes_short_id_and_interval() -> None:
    message = format_watch_list([_watch()])

    assert "M12345678" in message
    assert "Alice" in message
    assert "30 秒" in message


@pytest.mark.asyncio
async def test_watch_monitor_coalesces_due_rules_into_one_ocr_query() -> None:
    records = [_watch(), _watch("corporation", "Rat Nation")]
    repository = SimpleNamespace(
        due=AsyncMock(return_value=records),
        record_result=AsyncMock(side_effect=[True, True]),
        retry_notification=AsyncMock(),
    )
    sentry = SimpleNamespace(
        enabled=True,
        create_ocr_query=AsyncMock(
            return_value={"query_id": "ocrq_1", "requested_clients": ["node-1"]}
        ),
        wait_ocr_query_payload=AsyncMock(
            return_value={
                "expected_clients": 1,
                "received_clients": 1,
                "results": [
                    {
                        "system_name": "S-KSWL",
                        "names": ["Alice"],
                        "recognized": [
                            {
                                "name": "Alice",
                                "metadata": {"corporation_name": "Rat Nation"},
                            }
                        ],
                    }
                ],
            }
        ),
    )
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock())
    redis = fakeredis.aioredis.FakeRedis()
    monitor = SentryWatchMonitor(repository, sentry, qq, redis)

    try:
        await monitor.run_once()
    finally:
        await redis.aclose()

    sentry.create_ocr_query.assert_awaited_once_with({"mode": "all_nodes"})
    assert repository.record_result.await_count == 2
    assert qq.send_proactive_markdown.await_count == 2


@pytest.mark.asyncio
async def test_watch_monitor_reuses_recent_complete_snapshot() -> None:
    records = [_watch()]
    repository = SimpleNamespace(
        due=AsyncMock(return_value=records),
        record_result=AsyncMock(return_value=False),
        retry_notification=AsyncMock(),
    )
    payload = {
        "expected_clients": 1,
        "received_clients": 1,
        "results": [{"system_name": "S-KSWL", "names": ["Alice"]}],
    }
    sentry = SimpleNamespace(
        enabled=True,
        create_ocr_query=AsyncMock(
            return_value={"query_id": "ocrq_1", "requested_clients": ["node-1"]}
        ),
        wait_ocr_query_payload=AsyncMock(return_value=payload),
    )
    qq = SimpleNamespace(send_proactive_markdown=AsyncMock())
    redis = fakeredis.aioredis.FakeRedis()
    monitor = SentryWatchMonitor(
        repository,
        sentry,
        qq,
        redis,
        snapshot_reuse_seconds=30,
    )

    try:
        await monitor.run_once()
        await monitor.run_once()
    finally:
        await redis.aclose()

    sentry.create_ocr_query.assert_awaited_once_with({"mode": "all_nodes"})
    sentry.wait_ocr_query_payload.assert_awaited_once()
    assert repository.record_result.await_count == 2
