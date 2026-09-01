import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

from eve_risk.alerts import (
    ACTIVE_INTEL_STATE_KEY,
    ALERT_CURSOR_KEY,
    ALERT_EVENT_ID_KEY,
    SYSTEM_ALERT_STATE_KEY,
    SYSTEM_ALERT_STATE_READY_KEY,
    EveSentryAlertRelay,
    SentryAuthenticationError,
    _active_intel_map,
    _active_system_state,
    _personnel_movement_pairs,
    alert_subscription_action,
    format_active_intel_message,
    format_alert_message,
    format_monitoring_node_message,
    format_personnel_alert_message,
    format_system_alert_message,
    format_system_movement_message,
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
async def test_relay_persists_event_id_and_reuses_it_on_reconnect() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (
            b"id: evt-1\n"
            b"event: bootstrap\n"
            b'data: {"generated_at":"2026-08-30T08:00:00+00:00","active_intel":[],"alerts":[]}\n\n'
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = EveSentryAlertRelay(http, redis, SimpleNamespace(), "http://sentry.test/api/v1/events")
        await relay._stream_once()
        assert await redis.get(ALERT_EVENT_ID_KEY) == b"evt-1"
        await relay._stream_once()

    assert requests[0].headers.get("Last-Event-ID") is None
    assert requests[1].headers["Last-Event-ID"] == "evt-1"
    assert "since" not in requests[1].url.params
    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_surfaces_authentication_failures_separately() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = EveSentryAlertRelay(http, redis, SimpleNamespace(), "http://sentry.test/events")
        with pytest.raises(SentryAuthenticationError):
            await relay._stream_once()
    await redis.aclose()


@pytest.mark.asyncio
async def test_explicit_hostile_movement_event_is_deduplicated() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_text=AsyncMock())
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        payload = {
            "schema_version": "hostile_movement_event.v1",
            "movement_id": "move-1",
            "occurred_at": "2026-08-30T08:01:00+00:00",
            "from_system": {"name": "Jita"},
            "to_system": {"name": "Tama"},
            "hostile_count": 2,
            "personnel": [{"character_id": 1, "name": "Pilot"}],
            "source": "detector",
        }
        assert await relay.process_hostile_movement(payload) is True
        assert await relay.process_hostile_movement(payload) is True
        second = {**payload, "movement_id": "move-2"}
        assert await relay.process_hostile_movement(second) is True
    assert qq.send_proactive_text.await_count == 2
    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_does_not_ack_monitoring_event_after_delivery_failure() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            b"id: node-1\n"
            b"event: monitoring_node\n"
            b'data: {"changes":[{"change":"online","node_id":"node-a","system_name":"Jita"}]}\n\n'
            b"id: node-2\n"
            b"event: bootstrap\n"
            b'data: {"generated_at":"2026-08-30T08:00:00+00:00","active_intel":[],"alerts":[]}\n\n'
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=body,
        )

    async def fail_send(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("QQ unavailable")

    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_text=AsyncMock(side_effect=fail_send))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/api/v1/events",
        )
        await relay.subscribe("group-1")
        with pytest.raises(RuntimeError, match="processing failed"):
            await relay._stream_once()

    assert await redis.get(ALERT_EVENT_ID_KEY) is None
    assert qq.send_proactive_text.call_count == 1
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


def test_detector_count_uses_server_snapshot_when_active_roster_is_partial() -> None:
    raw_items = [
        {
            "id": f"ocr:{name.casefold()}",
            "active": True,
            "system_name": "S-KSWL",
            "name": name,
            "source": "eve-sentry-detector",
            "metadata": {"client_id": "detector-1", "hostile_icon_count": 4},
        }
        for name in ("Alpha", "Bravo", "Charlie")
    ]
    raw_alerts = [
        {
            "active_intel_id": item["id"],
            "classification": "red",
            "hostile_count": 4,
            "active_names": ["Alpha", "Bravo", "Charlie", "Delta"],
        }
        for item in raw_items
    ]

    mapped = _active_intel_map(raw_items, raw_alerts)
    assert {item["hostile_count"] for item in mapped.values()} == {4}
    state = _active_system_state(mapped.values(), "snapshot-1")

    assert state["s-kswl"]["hostile_count"] == 4


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
    assert format_system_alert_message("S-KSWL", "alert", 3) == "❗ S-KSWL 来敌"
    assert format_system_alert_message("S-KSWL", "safe") == "✅ S-KSWL 清空"
    assert format_system_movement_message("Jita", "Tama", 2) == (
        "🔵 敌对移动｜Jita → Tama｜当前敌对 2 人"
    )

    personnel = format_personnel_alert_message(
        {
            "system_name": "S-KSWL",
            "hostile_count": 1,
            "personnel": [item],
        },
        "2026-07-20T16:22:29+00:00",
    )
    assert "### ⚠️ 敌对事件" in personnel
    assert "**敌对**｜1 人" in personnel
    assert "**识别**｜1 人" in personnel
    assert "| 人员 | 星系 | zKill |" in personnel
    assert "| Alice | S-KSWL | https://zkillboard.com/character/12345/ |" in personnel
    assert "**时间**" not in personnel
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
    assert "| Alice | S-KSWL → Tama | https://zkillboard.com/character/12345/ |" in moved_personnel
    assert "**时间**" not in moved_personnel


def test_personnel_message_separates_detected_and_confirmed_counts() -> None:
    personnel = format_personnel_alert_message(
        {
            "system_name": "S-KSWL",
            "hostile_count": 3,
            "personnel": [
                {"name": "Alice", "character_id": 1001},
                {"name": "Bob", "character_id": 1002},
            ],
        },
        "2026-08-28T04:00:00+00:00",
    )

    assert "**敌对**｜3 人" in personnel
    assert "**识别**｜2 人" in personnel
    assert "| Alice | S-KSWL |" in personnel
    assert "| Bob | S-KSWL |" in personnel
    assert "**当前敌对**｜3 人" not in personnel


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
    assert "| Alice | S-KSWL | https://zkillboard.com/character/12345/ |" in message
    assert "Glory Navy" not in message
    assert "Fraternity." not in message
    assert "**时间**" not in message


def test_personnel_movement_matches_existing_destination_and_partial_identity() -> None:
    previous = {
        "jita": {
            "system_name": "Jita",
            "personnel": [{"name": "Tom Sisko", "character_id": 9001}],
        },
        "r-y": {
            "system_name": "R-Y",
            "personnel": [{"name": "Other Pilot", "character_id": 9002}],
        },
    }
    current = {
        "r-y": {
            "system_name": "R-Y",
            "personnel": [
                {"name": "Tom Sisko"},
                {"name": "Other Pilot", "character_id": 9002},
            ],
        }
    }

    pairs = _personnel_movement_pairs(previous, current)

    assert pairs == [
        {"from_system": "Jita", "to_system": "R-Y", "to_key": "r-y"}
    ]


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
        "character_id": 12345,
        "classification": "red",
        "metadata": {
            "client_id": "client-1",
            "identity_status": "resolved",
        },
    }

    state = _active_system_state([presence, ocr], "episode-1")["s-kswl"]

    assert state["hostile_count"] == 2
    assert [item["name"] for item in state["personnel"]] == ["Alice"]


def test_detector_personnel_waits_for_esi_identity_resolution() -> None:
    base = {
        "id": "ocr:alice",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-KSWL",
        "name": "Alice",
    }
    pending = {
        **base,
        "classification": "red",
        "metadata": {
            "client_id": "client-1",
            "identity_status": "pending",
        },
    }
    unresolved = {
        **base,
        "classification": "red",
        "metadata": {
            "client_id": "client-1",
            "identity_status": "unresolved",
        },
    }
    resolved = {
        **base,
        "character_id": 12345,
        "classification": "red",
        "metadata": {
            "client_id": "client-1",
            "identity_status": "resolved",
        },
    }

    assert _active_system_state([pending], "episode-1")["s-kswl"]["personnel"] == []
    assert _active_system_state([unresolved], "episode-1")["s-kswl"]["personnel"] == []
    personnel = _active_system_state([resolved], "episode-1")["s-kswl"]["personnel"]
    assert [(item["name"], item["character_id"]) for item in personnel] == [
        ("Alice", 12345)
    ]

    friendly = {
        **resolved,
        "classification": "white",
    }
    assert _active_system_state([friendly], "episode-1")["s-kswl"]["personnel"] == []


def test_named_detector_ocr_requires_corresponding_red_alert() -> None:
    item = {
        "id": "ocr:shazzza",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-K",
        "name": "Shazzza",
        "metadata": {
            "client_id": "client-1",
            "hostile_icon_count": 1,
        },
    }

    merged = _active_intel_map([item], [])

    assert merged == {}

    white = _active_intel_map(
        [item],
        [{"active_intel_id": "ocr:shazzza", "classification": "white"}],
    )
    assert white == {}

    red = _active_intel_map(
        [item],
        [{"active_intel_id": "ocr:shazzza", "classification": "red"}],
    )
    assert list(red) == ["ocr:shazzza"]
    assert red["ocr:shazzza"]["classification"] == "red"


def test_detector_aggregate_icon_count_does_not_include_friendly_roster() -> None:
    def item(name: str, character_id: int) -> dict[str, Any]:
        return {
            "id": f"ocr:{name.casefold()}",
            "active": True,
            "source": "eve-sentry-detector",
            "system_name": "S-KSWL",
            "name": name,
            "character_id": character_id,
            "metadata": {
                "client_id": "client-1",
                "hostile_icon_count": 2,
                "identity_status": "resolved",
            },
        }

    hostile = item("Hostile Pilot", 1001)
    friendly = item("Friendly Pilot", 1002)
    merged = _active_intel_map(
        [hostile, friendly],
        [
            {
                "active_intel_id": hostile["id"],
                "classification": "red",
            },
            {
                "active_intel_id": friendly["id"],
                "classification": "white",
            },
        ],
    )

    assert list(merged) == [hostile["id"]]
    state = _active_system_state(merged.values(), "episode-1")["s-kswl"]
    assert state["hostile_count"] == 1
    assert [person["name"] for person in state["personnel"]] == ["Hostile Pilot"]


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
            "❗ S-KSWL 来敌"
        ]
        qq.send_proactive_markdown.assert_not_awaited()

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_publishes_personnel_once_after_esi_resolution() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
    )
    presence = {
        "id": "presence:client-1:S-KSWL",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-KSWL",
        "metadata": {
            "client_id": "client-1",
            "presence_only": True,
            "hostile_icon_count": 1,
        },
    }
    pending = {
        "id": "ocr:alice",
        "active": True,
        "source": "eve-sentry-detector",
        "system_name": "S-KSWL",
        "name": "Al1ce",
        "metadata": {
            "client_id": "client-1",
            "hostile_icon_count": 1,
            "identity_status": "pending",
        },
    }
    resolved = {
        **pending,
        "name": "Alice",
        "character_id": 12345,
        "metadata": {
            **pending["metadata"],
            "identity_status": "resolved",
        },
    }

    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"generated_at": "t0", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t1",
                "active_intel": [presence, pending],
                "alerts": [],
            }
        )
        qq.send_proactive_markdown.assert_not_awaited()

        resolved_snapshot = {
            "generated_at": "t2",
            "active_intel": [presence, resolved],
            "alerts": [
                {
                    "id": "evt:alice",
                    "active_intel_id": "ocr:alice",
                    "classification": "red",
                    "level": "high",
                }
            ],
        }
        await relay.process_bootstrap(resolved_snapshot)
        await relay.process_bootstrap({**resolved_snapshot, "generated_at": "t3"})

        assert qq.send_proactive_text.await_count == 1
        assert qq.send_proactive_markdown.await_count == 1
        message = qq.send_proactive_markdown.await_args.args[1]
        assert "Alice" in message
        assert "Al1ce" not in message

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_does_not_enqueue_analysis_for_new_hostile_system() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(send_proactive_text=AsyncMock(return_value={"id": "text"}))
    enqueue = AsyncMock(return_value=True)
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/events",
        )
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"generated_at": "t0", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t1",
                "active_intel": [
                    {
                        "id": "ocr:alice",
                        "active": True,
                        "source": "eve-sentry-detector",
                        "system_name": "S-KSWL",
                        "name": "Alice",
                        "character_id": 12345,
                        "metadata": {
                            "client_id": "client-1",
                            "identity_status": "resolved",
                        },
                    }
                ],
                "alerts": [
                    {
                        "active_intel_id": "ocr:alice",
                        "classification": "red",
                    }
                ],
            }
        )

    enqueue.assert_not_awaited()
    assert qq.send_proactive_text.await_count == 2
    await redis.aclose()


@pytest.mark.asyncio
async def test_current_analysis_names_returns_confirmed_unique_personnel() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, SimpleNamespace(), "")
        await relay._save_system_alert_state(
            {
                "jita": {
                    "personnel": [
                        {"name": "Alice"},
                        {"name": "Unknown"},
                    ]
                },
                "tama": {
                    "personnel": [
                        {"name": " alice "},
                        {"name": "Bob"},
                        {"name": "Charlie"},
                    ]
                },
            }
        )

        assert await relay.current_analysis_names(2) == ["Alice", "Bob"]

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
        assert "在线监控节点｜2" in message
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
        assert "在线监控节点｜1" in qq.send_proactive_text.await_args.args[1]

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
            "character_id": 12345,
            "source": "eve-sentry-detector",
            "source_instance": "EVE - Hajimi6",
            "first_seen_at": "2026-07-20T16:20:24+00:00",
            "last_seen_at": "2026-07-20T16:20:24+00:00",
            "metadata": {"identity_status": "resolved"},
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
                    "classification": "red",
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
                    "classification": "red",
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
                    "classification": "red",
                    "level": "high",
                    "score": 80,
                },
                {
                    "active_intel_id": "ocr:bob",
                    "classification": "red",
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
                    "classification": "red",
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
                "❗ S-KSWL 来敌",
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
async def test_relay_compacts_complete_personnel_move_into_one_movement_message() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")

        def item(active_id: str, system_name: str) -> dict[str, Any]:
            return {
                "id": active_id,
                "active": True,
                "source": "eve-sentry-detector",
                "system_name": system_name,
                "name": "Alice",
                "character_id": 12345,
                "first_seen_at": "2026-08-24T10:00:00+00:00",
                "metadata": {
                    "client_id": "client-1",
                    "identity_status": "resolved",
                },
            }

        await relay.process_bootstrap(
            {"generated_at": "t0", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t1",
                "active_intel": [item("ocr:jita", "Jita")],
                "alerts": [
                    {
                        "active_intel_id": "ocr:jita",
                        "classification": "red",
                    }
                ],
            }
        )
        qq.send_proactive_text.reset_mock()
        qq.send_proactive_markdown.reset_mock()

        await relay.process_bootstrap(
            {
                "generated_at": "t2",
                "active_intel": [item("ocr:tama", "Tama")],
                "alerts": [
                    {
                        "active_intel_id": "ocr:tama",
                        "classification": "red",
                    }
                ],
            }
        )

        assert [call.args[1] for call in qq.send_proactive_text.await_args_list] == [
            "🔵 敌对移动｜Jita → Tama｜当前敌对 1 人"
        ]
        assert qq.send_proactive_markdown.await_count == 1
        assert "Jita → Tama" in qq.send_proactive_markdown.await_args.args[1]

    await redis.aclose()


@pytest.mark.asyncio
async def test_bootstrap_and_explicit_movement_events_share_cross_source_dedupe() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")

        def item(active_id: str, system_name: str) -> dict[str, Any]:
            return {
                "id": active_id,
                "active": True,
                "source": "eve-sentry-detector",
                "system_name": system_name,
                "name": "Alice",
                "character_id": 12345,
                "metadata": {
                    "client_id": "client-1",
                    "identity_status": "resolved",
                },
            }

        await relay.process_bootstrap(
            {"generated_at": "t0", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t1",
                "active_intel": [item("ocr:jita", "Jita")],
                "alerts": [{"active_intel_id": "ocr:jita", "classification": "red"}],
            }
        )
        qq.send_proactive_text.reset_mock()

        await relay.process_bootstrap(
            {
                "generated_at": "t2",
                "active_intel": [item("ocr:tama", "Tama")],
                "alerts": [{"active_intel_id": "ocr:tama", "classification": "red"}],
            }
        )
        assert qq.send_proactive_text.await_count == 1

        explicit = {
            "schema_version": "hostile_movement_event.v1",
            "movement_id": "detector-move-1",
            "occurred_at": "2026-08-30T08:01:00+00:00",
            "from_system": {"name": "Jita"},
            "to_system": {"name": "Tama"},
            "hostile_count": 1,
            "personnel": [{"character_id": 12345, "name": "Alice"}],
            "source": "detector",
        }
        assert await relay.process_hostile_movement(explicit) is True
        assert qq.send_proactive_text.await_count == 1

        await redis.flushdb()
        relay._active_alert_ids.clear()
        await relay.subscribe("group-1")
        qq.send_proactive_text.reset_mock()
        await relay.process_bootstrap(
            {"generated_at": "t3", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t4",
                "active_intel": [item("ocr:jita", "Jita")],
                "alerts": [{"active_intel_id": "ocr:jita", "classification": "red"}],
            }
        )
        qq.send_proactive_text.reset_mock()
        explicit = {**explicit, "movement_id": "detector-move-2"}
        assert await relay.process_hostile_movement(explicit) is True
        await relay.process_bootstrap(
            {
                "generated_at": "t5",
                "active_intel": [item("ocr:tama", "Tama")],
                "alerts": [{"active_intel_id": "ocr:tama", "classification": "red"}],
            }
        )
        assert qq.send_proactive_text.await_count == 1

    await redis.aclose()


@pytest.mark.asyncio
async def test_relay_does_not_enqueue_analysis_after_system_message_without_skipping_personnel(
) -> None:
    redis = fakeredis.aioredis.FakeRedis()
    delivery_order: list[str] = []

    async def send_text(_group: str, _message: str) -> dict[str, str]:
        delivery_order.append("event")
        return {"id": "event"}

    async def send_markdown(_group: str, _message: str) -> dict[str, str]:
        delivery_order.append("personnel")
        return {"id": "personnel"}

    qq = SimpleNamespace(
        send_proactive_text=send_text,
        send_proactive_markdown=send_markdown,
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/events",
        )
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"generated_at": "t0", "active_intel": [], "alerts": []}
        )
        await relay.process_bootstrap(
            {
                "generated_at": "t1",
                "active_intel": [
                    {
                        "id": "ocr:alice",
                        "active": True,
                        "source": "eve-sentry-detector",
                        "system_name": "S-KSWL",
                        "name": "Alice",
                        "character_id": 12345,
                        "metadata": {
                            "client_id": "client-1",
                            "identity_status": "resolved",
                        },
                    }
                ],
                "alerts": [
                    {
                        "active_intel_id": "ocr:alice",
                        "classification": "red",
                    }
                ],
            }
        )

    assert delivery_order[0] == "event"
    assert "analysis" not in delivery_order
    assert delivery_order.count("personnel") == 1
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
        stored = await redis.hget(SYSTEM_ALERT_STATE_KEY, "s-kswl")
        assert stored is not None
        stored_state = json.loads(stored)
        stored_state["personnel_fingerprint"] = "legacy-hidden-field-fingerprint"
        await redis.hset(
            SYSTEM_ALERT_STATE_KEY,
            "s-kswl",
            json.dumps(stored_state),
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
async def test_personnel_push_interval_coalesces_to_latest_roster() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/events",
            personnel_push_interval_seconds=0.05,
        )
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"active_intel": [], "alerts": [], "generated_at": "t0"}
        )

        def bootstrap(*names: str, generated_at: str) -> dict[str, Any]:
            active_intel = [
                {
                    "id": f"ocr:{name.casefold()}",
                    "active": True,
                    "system_name": "S-KSWL",
                    "name": name,
                }
                for name in names
            ]
            return {
                "active_intel": active_intel,
                "alerts": [
                    {"active_intel_id": item["id"], "level": "high"}
                    for item in active_intel
                ],
                "generated_at": generated_at,
            }

        await relay.process_bootstrap(bootstrap("Alice", generated_at="t1"))
        await relay.process_bootstrap(bootstrap("Alice", "Bob", generated_at="t2"))
        await relay.process_bootstrap(
            bootstrap("Alice", "Bob", "Charlie", generated_at="t3")
        )

        assert qq.send_proactive_markdown.await_count == 1
        await asyncio.sleep(0.08)
        assert qq.send_proactive_markdown.await_count == 2
        latest_message = qq.send_proactive_markdown.await_args_list[-1].args[1]
        assert "Alice" in latest_message
        assert "Bob" in latest_message
        assert "Charlie" in latest_message

    await redis.aclose()


@pytest.mark.asyncio
async def test_personnel_push_interval_discards_stale_roster_after_clear() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(
            http,
            redis,
            qq,
            "http://sentry.test/events",
            personnel_push_interval_seconds=0.05,
        )
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"active_intel": [], "alerts": [], "generated_at": "t0"}
        )
        alice = {
            "id": "ocr:alice",
            "active": True,
            "system_name": "S-KSWL",
            "name": "Alice",
        }
        bob = {**alice, "id": "ocr:bob", "name": "Bob"}
        await relay.process_bootstrap(
            {
                "active_intel": [alice],
                "alerts": [{"active_intel_id": "ocr:alice", "level": "high"}],
                "generated_at": "t1",
            }
        )
        await relay.process_bootstrap(
            {
                "active_intel": [alice, bob],
                "alerts": [
                    {"active_intel_id": "ocr:alice", "level": "high"},
                    {"active_intel_id": "ocr:bob", "level": "high"},
                ],
                "generated_at": "t2",
            }
        )
        await relay.process_bootstrap(
            {"active_intel": [], "alerts": [], "generated_at": "t3"}
        )

        await asyncio.sleep(0.08)
        assert qq.send_proactive_markdown.await_count == 1
        assert not relay._personnel_pending

    await redis.aclose()


@pytest.mark.asyncio
async def test_hidden_threat_enrichment_does_not_repeat_personnel_alert() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    qq = SimpleNamespace(
        send_proactive_markdown=AsyncMock(return_value={"id": "markdown"}),
        send_proactive_text=AsyncMock(return_value={"id": "text"}),
    )
    async with httpx.AsyncClient() as http:
        relay = EveSentryAlertRelay(http, redis, qq, "http://sentry.test/events")
        await relay.subscribe("group-1")
        await relay.process_bootstrap(
            {"active_intel": [], "alerts": [], "generated_at": "t0"}
        )
        alice = {
            "id": "ocr:alice",
            "active": True,
            "source": "eve-sentry-detector",
            "system_name": "S-KSWL",
            "name": "Alice",
            "character_id": 12345,
            "metadata": {
                "client_id": "client-1",
                "identity_status": "resolved",
            },
        }
        await relay.process_bootstrap(
            {
                "active_intel": [{**alice, "character_id": 12345}],
                "alerts": [
                    {
                        "active_intel_id": "ocr:alice",
                        "classification": "red",
                        "level": "medium",
                        "score": 55,
                    }
                ],
                "generated_at": "t1",
            }
        )
        await relay.process_bootstrap(
            {
                "active_intel": [alice],
                "alerts": [
                    {
                        "active_intel_id": "ocr:alice",
                        "classification": "red",
                        "level": "high",
                        "score": 80,
                        "metadata": {
                            "corporation_name": "Enriched Corporation",
                            "alliance_name": "Enriched Alliance",
                        },
                    }
                ],
                "generated_at": "t2",
            }
        )

        assert qq.send_proactive_markdown.await_count == 1
        assert qq.send_proactive_text.await_count == 1

    await redis.aclose()


@pytest.mark.asyncio
async def test_alert_event_delivers_system_alert_without_legacy_template() -> None:
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
                "hostile_count": 2,
                "created_at": "2026-08-10T01:00:03+00:00",
            }
        )

        qq.send_proactive_markdown.assert_not_awaited()
        assert [call.args[1] for call in qq.send_proactive_text.await_args_list] == [
            "❗ S-KSWL 来敌"
        ]
        assert await redis.get(ALERT_CURSOR_KEY) == b"2026-08-10T01:00:03+00:00"

        await relay.process_alert_event(
            {
                "id": "evt:transient",
                "active": True,
                "system_name": "S-KSWL",
                "name": "Alice",
                "hostile_count": 2,
                "created_at": "2026-08-10T01:00:03+00:00",
            }
        )
        assert qq.send_proactive_text.await_count == 1

    await redis.aclose()


@pytest.mark.asyncio
async def test_presence_alert_event_is_not_repeated_by_later_bootstrap() -> None:
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
                "id": "presence:event-1",
                "active": True,
                "system_name": "S-KSWL",
                "hostile_count": 1,
                "presence_only": True,
                "source_observation_id": "presence:client-1:S-KSWL",
                "created_at": "2026-08-10T01:00:02+00:00",
            }
        )
        await relay.process_bootstrap(
            {
                "generated_at": "2026-08-10T01:00:03+00:00",
                "active_intel": [
                    {
                        "id": "presence:client-1:S-KSWL",
                        "active": True,
                        "source": "eve-sentry-detector",
                        "system_name": "S-KSWL",
                        "metadata": {
                            "presence_only": True,
                            "hostile_icon_count": 1,
                            "client_id": "client-1",
                        },
                    }
                ],
                "alerts": [],
            }
        )

    assert [call.args[1] for call in qq.send_proactive_text.await_args_list] == [
        "❗ S-KSWL 来敌"
    ]
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
