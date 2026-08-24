import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import (
    ACTIVE_INTEL_STATE_KEY,
    ALERT_CURSOR_KEY,
    SYSTEM_ALERT_STATE_KEY,
    SYSTEM_ALERT_STATE_READY_KEY,
    EveSentryAlertRelay,
    _active_intel_map,
    _active_system_state,
    alert_subscription_action,
    format_active_intel_message,
    format_alert_message,
    format_monitoring_node_message,
    format_personnel_alert_message,
    format_system_alert_message,
    iter_sse_events,
)


async def _sse_lines():
    for line in (
        ": keepalive",
        "",
        "id: evt-1",
        "event: alert",
        'data: {"id":"evt-1"}',
        "",
    ):
        yield line


@pytest.mark.asyncio
async def test_relay_stream_uses_long_lived_sse_request() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b": keepalive\n\n",
        )

    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            SimpleNamespace(),
            "http://sentry.test/api/v1/events",
        )
        await relay._stream_once()

    assert len(requests) == 1
    assert "timeout" not in requests[0].url.params
    assert requests[0].url.params["heartbeat"] == "15"
    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_reconnects_immediately_after_clean_eof() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            SimpleNamespace(),
            "http://sentry.test/api/v1/events",
        )
        relay._stream_once = AsyncMock(
            side_effect=[None, asyncio.CancelledError()]
        )
        with pytest.raises(asyncio.CancelledError):
            await relay.run_forever()

    assert relay._stream_once.await_count == 2
    await redis.aclose()


@pytest.mark.asyncio
async def test_iter_sse_events_ignores_comments_and_parses_alerts() -> None:
    events = [event async for event in iter_sse_events(_sse_lines())]

    assert events == [("alert", "evt-1", '{"id":"evt-1"}')]


def test_alert_subscription_commands_and_message_format() -> None:
    assert alert_subscription_action("开启预警") == "enable"
    assert alert_subscription_action("<@!bot> 关闭预警") == "disable"
    assert alert_subscription_action("预警状态") == "status"
    assert alert_subscription_action("分析 Alice") is None

    item = {
        "id": "ocr:alice",
        "_remaining_count": 2,
        "system_name": "S-KSWL",
        "name": "Alice",
        "character_id": 12345,
        "source": "eve-sentry-detector",
        "source_instance": "EVE - Hajimi6",
        "level": "high",
        "score": 80,
        "first_seen_at": "2026-07-20T16:20:24+00:00",
        "metadata": {
            "alliance_ticker": "FRT",
            "alliance_name": "Fraternity.",
            "corporation_ticker": "G.N.V",
            "corporation_name": "Glory Navy",
        },
    }
    entered = format_active_intel_message(
        item,
        "entered",
        "2026-07-20T16:20:24+00:00",
        "http://sentry.test/",
    )
    left = format_active_intel_message(
        {**item, "_remaining_count": 0},
        "left",
        "2026-07-20T16:22:29+00:00",
        "http://sentry.test/",
    )

    assert entered.splitlines()[0] == "### 🔴 敌对进入｜当前敌对 2 人"
    assert "**位置**｜S-KSWL" in entered
    assert "**目标**｜Alice" in entered
    assert "**联盟**｜[FRT] Fraternity." in entered
    assert "**军团**｜[G.N.V] Glory Navy" in entered
    assert "**威胁**｜高（评分 80）" in entered
    assert "**来源**" not in entered
    assert "**进入时间**｜2026-07-21 00:20:24" in entered
    assert "状态" not in entered
    assert "态势图" not in entered
    assert left.splitlines()[0] == "### 🟢 敌对离开｜当前敌对 0 人"
    assert "**联盟**｜[FRT] Fraternity." in left
    assert "**军团**｜[G.N.V] Glory Navy" in left
    assert "**来源**" not in left
    assert "**离开时间**｜2026-07-21 00:22:29" in left
    assert "**停留**｜2 分 5 秒" in left
    assert "状态" not in left
    assert "态势图" not in left
    assert "敌对进入" in format_alert_message(item)
    assert format_system_alert_message("S-KSWL", "alert") == "❗ S-KSWL 来敌"
    assert format_system_alert_message("S-KSWL", "alert", 3) == "❗ S-KSWL 来敌｜当前敌对 3 人"
    assert format_system_alert_message("S-KSWL", "safe") == "✅ S-KSWL 清空"

    personnel = format_personnel_alert_message(
        {
            "system_name": "S-KSWL",
            "hostile_count": 1,
            "personnel": [item],
        },
        "2026-07-20T16:22:29+00:00",
    )
    assert "### ⚠️ 敌对事件" in personnel
    assert "**当前敌对**｜1 人" in personnel
    assert "| 人员 | 星系 | 军团 | 联盟 | 时间 | zKill |" in personnel
    assert "| Alice | S-KSWL | G.N.V | FRT | 2026-07-21 00:20:24 | [🔗](https://zkillboard.com/character/12345/) |" in personnel
    moved_personnel = format_personnel_alert_message(
        {
            "system_name": "Tama",
            "hostile_count": 1,
            "personnel": [
                {
                    "name": "Alice",
                    "character_id": 12345,
                    "system_display": "S-KSWL → Tama",
                    "metadata": {
                        "corporation_ticker": "G.N.V",
                        "alliance_ticker": "FRT",
                    },
                }
            ],
        },
        "2026-07-20T16:22:29+00:00",
    )
    assert "| Alice | S-KSWL → Tama | G.N.V | FRT | 2026-07-21 00:22:29 | [🔗](https://zkillboard.com/character/12345/) |" in moved_personnel


def test_personnel_affiliations_fall_back_to_alert_metadata_and_profiles() -> None:
    item = {
        "id": "ocr:alice",
        "active": True,
        "system_name": "S-KSWL",
        "name": "Alice",
        "character_id": 12345,
    }
    merged = _active_intel_map(
        [item],
        [
            {
                "active_intel_id": "ocr:alice",
                "metadata": {
                    "character_profiles": [
                        {
                            "character_id": 12345,
                            "corporation_name": "Glory Navy",
                            "alliance_name": "Fraternity.",
                        }
                    ]
                },
            }
        ],
    )
    state = _active_system_state(merged.values(), "episode-1")["s-kswl"]
    message = format_personnel_alert_message(
        state,
        "2026-07-20T16:22:29+00:00",
    )
    assert "| Alice | S-KSWL | Glory Navy | Fraternity. |" in message


def test_presence_only_intel_creates_system_alert_without_personnel_row() -> None:
    item = {
        "id": "presence:client-1:S-KSWL",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-KSWL",
        "metadata": {
            "presence_only": True,
            "hostile_icon_count": 2,
            "client_id": "client-1",
        },
    }

    merged = _active_intel_map([item], [])

    assert list(merged) == ["presence:client-1:S-KSWL"]
    state = _active_system_state(merged.values(), "episode-1")["s-kswl"]
    assert state["hostile_count"] == 2
    assert state["personnel"] == []
    assert state["personnel_fingerprint"] == ""


def test_presence_and_ocr_for_one_detector_are_not_double_counted() -> None:
    presence = {
        "id": "presence:client-1:S-KSWL",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-KSWL",
        "metadata": {
            "presence_only": True,
            "hostile_icon_count": 2,
            "client_id": "client-1",
        },
    }
    ocr = {
        "id": "ocr:alice",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-KSWL",
        "name": "Alice",
        "metadata": {"client_id": "client-1"},
    }

    state = _active_system_state([presence, ocr], "episode-1")["s-kswl"]

    assert state["hostile_count"] == 2
    assert [item["name"] for item in state["personnel"]] == ["Alice"]


@pytest.mark.asyncio
async def test_relay_delivers_presence_only_system_alert_without_empty_personnel_table() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"generated_at": "t0", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t1",
                "active_intel": [
                    {
                        "id": "presence:client-1:S-KSWL",
                        "active": True,
                        "source": "eve-sentry-detector",
                        "system_name": "S-KSWL",
                        "metadata": {
                            "presence_only": True,
                            "hostile_icon_count": 2,
                        },
                    }
                ],
                "alerts": [],
            }
        )

        assert [call.args[1] for call in qq.send_proactive_text.await_args_list] == [
            "❗ S-KSWL 来敌｜当前敌对 2 人"
        ]
        qq.send_proactive_markdown.assert_not_awaited()

    await redis.aclose()


def test_monitoring_node_message_formats_online_offline_and_move() -> None:
    assert format_monitoring_node_message(
        {
            "change": "online",
            "character_name": "Pilot Alpha",
            "system_name": "Jita",
        }
    ) == "🟢 监控节点上线\n位置｜Jita"
    assert format_monitoring_node_message(
        {
            "change": "offline",
            "character_name": "Pilot Alpha",
            "system_name": "Jita",
        }
    ) == "⚪ 监控节点下线\n最后位置｜Jita"
    assert format_monitoring_node_message(
        {
            "change": "moved",
            "character_name": "Pilot Alpha",
            "from_system": "Jita",
            "to_system": "Tama",
        }
    ) == "🔵 监控节点移动\n去向｜Jita → Tama"


@pytest.mark.asyncio
async def test_relay_delivers_monitoring_node_changes_once_per_group() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(return_value={"id": "m1"}),
    )
    payload = {
        "generated_at": "2026-08-10T01:00:00+00:00",
        "changes": [
            {
                "change": "online",
                "node_id": "client:alpha",
                "character_name": "Pilot Alpha",
                "system_name": "Jita",
            },
            {
                "change": "moved",
                "node_id": "client:beta",
                "character_name": "Pilot Beta",
                "from_system": "Amarr",
                "to_system": "Tama",
                "system_name": "Tama",
            },
            {
                "change": "offline",
                "node_id": "client:gamma",
                "character_name": "Pilot Gamma",
                "system_name": "Dodixie",
            },
        ],
    }
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")

        await relay.process_monitoring_node(payload)
        await relay.process_monitoring_node(payload)
        await relay.process_bootstrap(
            {
                "generated_at": payload["generated_at"],
                "monitoring_node_changes": payload["changes"],
                "active_intel": [],
                "alerts": [],
            }
        )

        assert [call.args[1] for call in qq.send_proactive_text.await_args_list] == [
            "🟢 监控节点上线\n位置｜Jita",
                "🔵 监控节点移动\n去向｜Amarr → Tama",
            "⚪ 监控节点下线\n最后位置｜Dodixie",
        ]

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_pushes_full_node_snapshot_and_recovers_after_missed_event() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(return_value={"id": "snapshot"}),
    )
    payload = {
        "generated_at": "2026-08-10T01:00:00+00:00",
        "nodes_version": "v1",
        "nodes": [
            {
                "client_id": "client:alpha",
                "character_name": "Pilot Alpha",
                "system_name": "Jita",
            },
            {
                "client_id": "client:beta",
                "character_name": "Pilot Beta",
                "system_name": "Tama",
            },
        ],
        "changes": [
            {
                "change": "moved",
                "node_id": "client:alpha",
                "from_system": "Amarr",
                "to_system": "Jita",
                "system_name": "Jita",
            }
        ],
    }
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")

        await relay.process_monitoring_node(payload)
        await relay.process_bootstrap(
            {
                "generated_at": payload["generated_at"],
                "monitoring_nodes": payload["nodes"],
                "monitoring_nodes_version": "v1",
                "monitoring_node_changes": payload["changes"],
                "active_intel": [],
                "alerts": [],
            }
        )
        assert qq.send_proactive_text.await_count == 1
        message = qq.send_proactive_text.await_args.args[1]
        assert "在线节点｜2" in message
        assert "监控节点 1｜Jita" in message
        assert "监控节点 2｜Tama" in message
        assert "监控节点状态更新" not in message
        assert "变化｜" not in message
        assert "Pilot Alpha" not in message
        assert "Pilot Beta" not in message

        await relay.process_bootstrap(
            {
                "generated_at": "2026-08-10T01:00:05+00:00",
                "monitoring_nodes": [payload["nodes"][1]],
                "monitoring_nodes_version": "v2",
                "monitoring_node_changes": [],
                "active_intel": [],
                "alerts": [],
            }
        )
        assert qq.send_proactive_text.await_count == 2
        assert "在线节点｜1" in qq.send_proactive_text.await_args.args[1]

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_pushes_only_system_entry_and_clear_transitions() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "m1"}),
        send_proactive_text=AsyncMock(return_value={"id": "m2"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/api/v1/events",
            api_key="eve_service_secret",
            public_url="http://sentry.test",
        )
        assert relay.api_key == "eve_service_secret"
        await relay.subscribe("group-1")
        assert await relay.is_subscribed("group-1") is True

        item = {
            "id": "ocr:alice",
            "active": True,
            "system_name": "S-KSWL",
            "name": "Alice",
            "source": "eve-sentry-detector",
            "source_instance": "EVE - Hajimi6",
            "first_seen_at": "2026-07-20T16:20:24+00:00",
            "last_seen_at": "2026-07-20T16:20:24+00:00",
        }
        second_item = {
            **item,
            "id": "ocr:bob",
            "name": "Bob",
        }
        existing_bootstrap = {
            "generated_at": "2026-07-20T16:20:24+00:00",
            "active_intel": [
                item,
                {
                    **item,
                    "id": "ocr:friendly",
                    "name": "Friendly Pilot",
                },
            ],
            "alerts": [
                {
                    "active_intel_id": "ocr:alice",
                    "level": "high",
                    "score": 80,
                },
            ],
        }
        cleared_bootstrap = {
            "generated_at": "2026-07-20T16:21:00+00:00",
            "active_intel": [],
            "alerts": [],
        }
        entered_bootstrap = {
            "generated_at": "2026-07-20T16:22:00+00:00",
            "active_intel": [item],
            "alerts": [
                {
                    "active_intel_id": "ocr:alice",
                    "level": "high",
                    "score": 80,
                },
            ],
        }
        count_increased_bootstrap = {
            **entered_bootstrap,
            "generated_at": "2026-07-20T16:22:30+00:00",
            "active_intel": [
                {**item, "last_seen_at": "2026-07-20T16:20:30+00:00"},
                {**second_item, "last_seen_at": "2026-07-20T16:20:30+00:00"},
            ],
            "alerts": [
                {
                    "active_intel_id": "ocr:alice",
                    "level": "high",
                    "score": 80,
                },
                {
                    "active_intel_id": "ocr:bob",
                    "level": "medium",
                    "score": 55,
                },
            ],
        }
        partial_left_bootstrap = {
            "generated_at": "2026-07-20T16:23:00+00:00",
            "active_intel": [
                {**second_item, "last_seen_at": "2026-07-20T16:23:00+00:00"},
            ],
            "alerts": [
                {
                    "active_intel_id": "ocr:bob",
                    "level": "medium",
                    "score": 55,
                },
            ],
        }
        left_bootstrap = {
            "generated_at": "2026-07-20T16:24:00+00:00",
            "active_intel": [],
            "alerts": [],
        }

        # The first bootstrap establishes a baseline and never replays history.
        await relay.process_bootstrap(existing_bootstrap)
        qq.send_proactive_text.assert_not_awaited()

        await relay.process_bootstrap(cleared_bootstrap)
        await relay.process_bootstrap(entered_bootstrap)
        await relay.process_bootstrap(count_increased_bootstrap)
        await relay.process_bootstrap(partial_left_bootstrap)
        await relay.process_bootstrap(left_bootstrap)
        await relay.process_bootstrap(left_bootstrap)

        assert [call.args[1] for call in qq.send_proactive_text.await_args_list] == [
            "✅ S-KSWL 清空",
                "❗ S-KSWL 来敌｜当前敌对 1 人",
            "✅ S-KSWL 清空",
        ]
        assert qq.send_proactive_markdown.await_count == 3
        assert all(
            "### ⚠️ 敌对事件" in call.args[1]
            for call in qq.send_proactive_markdown.await_args_list
        )
        assert await redis.hlen(ACTIVE_INTEL_STATE_KEY) == 0
        assert await redis.hlen(SYSTEM_ALERT_STATE_KEY) == 0
        assert await redis.get(SYSTEM_ALERT_STATE_READY_KEY) == b"1"
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-07-20T16:24:00+00:00"

        await relay.unsubscribe("group-1")
        assert await relay.is_subscribed("group-1") is False

    await redis.aclose()


@pytest.mark.asyncio
async def test_system_transition_retries_before_advancing_state() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(
            side_effect=[RuntimeError("temporary failure"), {"id": "m1"}]
        ),
        send_proactive_markdown=AsyncMock(return_value={"id": "personnel"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {
                "generated_at": "2026-07-20T16:20:00+00:00",
                "active_intel": [],
                "alerts": [],
            }
        )
        hostile_bootstrap = {
            "generated_at": "2026-07-20T16:21:00+00:00",
            "active_intel": [
                {
                    "id": "ocr:alice",
                    "active": True,
                    "system_name": "S-KSWL",
                    "name": "Alice",
                }
            ],
            "alerts": [{"active_intel_id": "ocr:alice", "level": "high"}],
        }

        await relay.process_bootstrap(hostile_bootstrap)
        assert await redis.hlen(SYSTEM_ALERT_STATE_KEY) == 0
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-07-20T16:20:00+00:00"

        await relay.process_bootstrap(hostile_bootstrap)

        assert qq.send_proactive_text.await_count == 2
        assert await redis.hlen(SYSTEM_ALERT_STATE_KEY) == 1
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-07-20T16:21:00+00:00"

    await redis.aclose()


@pytest.mark.asyncio
async def test_personnel_updates_are_once_per_episode_and_fingerprint() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")

        alice = {
            "id": "ocr:alice",
            "active": True,
            "system_name": "S-KSWL",
            "name": "Alice",
        }
        bob = {**alice, "id": "ocr:bob", "name": "Bob"}
        empty = {"active_intel": [], "alerts": [], "generated_at": "t0"}

        await relay.process_bootstrap(empty)
        await relay.process_bootstrap(
            {
                "active_intel": [alice],
                "alerts": [{"active_intel_id": "ocr:alice", "level": "high"}],
                "generated_at": "t1",
            }
        )
        await relay.process_bootstrap(
            {
                "active_intel": [alice],
                "alerts": [{"active_intel_id": "ocr:alice", "level": "high"}],
                "generated_at": "t2",
            }
        )
        assert qq.send_proactive_markdown.await_count == 1

        await relay.process_bootstrap(
            {
                "active_intel": [alice, bob],
                "alerts": [
                    {"active_intel_id": "ocr:alice", "level": "high"},
                    {"active_intel_id": "ocr:bob", "level": "medium"},
                ],
                "generated_at": "t3",
            }
        )
        assert qq.send_proactive_markdown.await_count == 2
        assert "Alice" in qq.send_proactive_markdown.await_args_list[-1].args[1]
        assert "Bob" in qq.send_proactive_markdown.await_args_list[-1].args[1]

        await relay.process_bootstrap({**empty, "generated_at": "t4"})
        await relay.process_bootstrap(
            {
                "active_intel": [alice],
                "alerts": [{"active_intel_id": "ocr:alice", "level": "high"}],
                "generated_at": "t5",
            }
        )
        assert qq.send_proactive_markdown.await_count == 3
        assert qq.send_proactive_text.await_count == 3
        assert qq.send_proactive_text.await_args_list[-2].args[1].startswith("✅ S-KSWL 清空")
        assert qq.send_proactive_text.await_args_list[-1].args[1].startswith("❗ S-KSWL 来敌")

    await redis.aclose()


@pytest.mark.asyncio
async def test_legacy_alert_event_does_not_send_old_template() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {
                "generated_at": "2026-08-10T01:00:00+00:00",
                "active_intel": [],
                "alerts": [],
            }
        )

        await relay.process_alert_event(
            {
                "id": "evt:transient",
                "active": True,
                "system_name": "S-KSWL",
                "name": "Alice",
                "created_at": "2026-08-10T01:00:03+00:00",
            }
        )

        qq.send_proactive_markdown.assert_not_awaited()
        qq.send_proactive_text.assert_not_awaited()
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-08-10T01:00:00+00:00"

        await relay.process_alert_event(
            {
                "id": "evt:transient",
                "active": True,
                "system_name": "S-KSWL",
                "name": "Alice",
                "created_at": "2026-08-10T01:00:03+00:00",
            }
        )
        qq.send_proactive_markdown.assert_not_awaited()

    await redis.aclose()


@pytest.mark.asyncio
async def test_alert_event_in_active_bootstrap_is_not_delivered_twice() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {
                "generated_at": "2026-08-10T01:00:00+00:00",
                "active_intel": [],
                "alerts": [],
            }
        )
        await relay.process_bootstrap(
            {
                "generated_at": "2026-08-10T01:00:01+00:00",
                "active_intel": [
                    {
                        "id": "ocr:alice",
                        "active": True,
                        "system_name": "S-KSWL",
                        "name": "Alice",
                    }
                ],
                "alerts": [
                    {
                        "id": "evt:alice",
                        "active_intel_id": "ocr:alice",
                        "level": "high",
                    }
                ],
            }
        )
        await relay.process_alert_event(
            {
                "id": "evt:alice",
                "active_intel_id": "ocr:alice",
                "active": True,
                "system_name": "S-KSWL",
                "name": "Alice",
                "created_at": "2026-08-10T01:00:01+00:00",
            }
        )
        assert qq.send_proactive_markdown.await_count == 1

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_falls_back_to_plain_text_when_markdown_is_unavailable() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(side_effect=RuntimeError("unsupported")),
        send_proactive_text=AsyncMock(return_value={"id": "m1"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.deliver(
            {
                "id": "ocr:alice",
                "system_name": "S-KSWL",
                "name": "Alice",
                "first_seen_at": "2026-07-20T16:20:24+00:00",
            },
            "entered",
            "2026-07-20T16:20:24+00:00",
        )

        fallback_message = qq.send_proactive_text.await_args.args[1]
        assert fallback_message.startswith("🔴 敌对进入")
        assert "**" not in fallback_message

    await redis.aclose()
