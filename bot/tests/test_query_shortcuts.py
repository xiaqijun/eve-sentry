"""Direct commands and scoped repeat queries must avoid menu round trips."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import botpy
import fakeredis.aioredis
import pytest

from eve_risk.bot import RiskBotClient
from eve_risk.query_history import recall_query, remember_query
from eve_risk.sentry_status import SentryStatusError, parse_sentry_query


@pytest.mark.parametrize("prefix", ["", "/", "@哨兵 ", "@哨兵/", "<@!bot-user>/"])
@pytest.mark.parametrize(("command", "expected"), [
    ("查人 Alice Example", {"mode": "filtered", "name": "Alice Example"}),
    ("查角色：Alice", {"mode": "filtered", "name": "Alice"}),
    ("查军团 Blue Corp", {"mode": "filtered", "corporation": "Blue Corp"}),
    ("查联盟 : Example Alliance", {"mode": "filtered", "alliance": "Example Alliance"}),
    ("查星系 S-KSWL", {"mode": "system_roster", "system_name": "S-KSWL"}),
    ("查询 星系：S-KSWL", {"mode": "system_roster", "system_name": "S-KSWL"}),
    ("敌情", {"mode": "node_hostiles"}),
    ("节点", {"mode": "monitoring_nodes"}),
    ("查名单", {"mode": "all_nodes"}),
    ("再查", {"mode": "repeat"}),
    ("刷新查询", {"mode": "repeat"}),
    ("查询菜单", {"mode": "menu"}),
])
def test_shortcuts_normalize_qq_prefixes(prefix, command, expected):
    assert parse_sentry_query(prefix + command) == expected


@pytest.mark.parametrize("command", ["查人员名单很麻烦", "再查一下吧", "节点异常了", "Alice", "分析 Alice", "开启预警"])
def test_shortcuts_do_not_capture_other_messages(command):
    assert parse_sentry_query(command) is None


@pytest.fixture
async def client():
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    client.redis = fakeredis.aioredis.FakeRedis()
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})
    client.qq.send_markdown = AsyncMock(return_value={"id": "menu"})
    client.qq.send_proactive_markdown = AsyncMock(return_value={"id": "result"})
    client.sentry_status.query = AsyncMock(return_value="当前结果")
    client.sentry_status.create_ocr_query = AsyncMock(return_value={
        "query_id": "query-1", "requested_clients": ["node-1"],
    })
    client.sentry_status.wait_ocr_query_payload = AsyncMock(return_value={"results": []})
    yield client
    if client.query_tasks:
        await asyncio.gather(*client.query_tasks)
    await client.redis.aclose()
    await original_redis.aclose()
    await client.http_client.aclose()


def message(text, message_id="m1", group="g1", member="u1"):
    return SimpleNamespace(id=message_id, group_openid=group, content=text,
                           author=SimpleNamespace(member_openid=member))


async def finish_queries(client):
    if client.query_tasks:
        await asyncio.gather(*client.query_tasks)


async def test_repeat_requests_new_ocr_without_menu_and_deduplicates_delivery(client):
    await client.on_group_at_message_create(message("查星系 S-KSWL"))
    await finish_queries(client)
    await client.on_group_at_message_create(message("再查", "m2"))
    await finish_queries(client)
    await client.on_group_at_message_create(message("再查", "m2"))
    calls = client.sentry_status.create_ocr_query.await_args_list
    assert len(calls) == 2
    assert all(call.args == ({"mode": "system_roster", "system_name": "S-KSWL"},) for call in calls)
    assert client.sentry_status.wait_ocr_query_payload.await_count == 2
    client.qq.send_markdown.assert_not_awaited()
    client.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("command", ["敌情", "节点"])
async def test_snapshot_shortcuts_and_repeat_never_start_ocr(client, command):
    await client.on_group_at_message_create(message(command))
    await client.on_group_at_message_create(message("再查", "m2"))
    assert client.sentry_status.query.await_count == 2
    client.sentry_status.create_ocr_query.assert_not_awaited()
    client.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize(("group", "member"), [("g2", "u1"), ("g1", "u2")])
async def test_repeat_is_isolated_by_group_and_member(client, group, member):
    await client.on_group_at_message_create(message("查人 Alice"))
    await finish_queries(client)
    await client.on_group_at_message_create(message("再查", "m2", group, member))
    assert client.sentry_status.create_ocr_query.await_count == 1
    assert "没有最近 10 分钟" in client.qq.send_text.await_args.args[2]


async def test_history_expires_and_stores_no_raw_openids(client):
    await remember_query(client.redis, "g1", "u1", {"mode": "filtered", "name": "Alice"})
    keys = await client.redis.keys("qq:query:last:*")
    assert len(keys) == 1
    assert 0 < await client.redis.ttl(keys[0]) <= 600
    raw = await client.redis.get(keys[0])
    assert b"g1" not in raw and b"u1" not in raw
    await client.redis.expire(keys[0], 0)
    await client.on_group_at_message_create(message("再查"))
    client.sentry_status.create_ocr_query.assert_not_awaited()
    assert "没有最近 10 分钟" in client.qq.send_text.await_args.args[2]


async def test_busy_failed_and_menu_requests_do_not_overwrite_history(client):
    original = {"mode": "filtered", "name": "Alice"}
    await remember_query(client.redis, "g1", "u1", original)
    await client.on_group_at_message_create(message("查询"))
    client._start_sentry_ocr_query = AsyncMock(return_value=False)
    await client.on_group_at_message_create(message("查人 Bob", "m2"))
    client.sentry_status.query = AsyncMock(side_effect=SentryStatusError("服务不可用"))
    await client.on_group_at_message_create(message("敌情", "m3"))
    assert await recall_query(client.redis, "g1", "u1") == original


@pytest.mark.parametrize("command", ["查人", "查人：", "查询 人员", "查军团", "查联盟：", "查星系："])
async def test_missing_target_never_sends_http_or_overwrites_history(client, command):
    # Restore the real method so validation is covered before its HTTP call.
    from eve_risk.sentry_status import EveSentryStatusClient
    client.sentry_status.create_ocr_query = EveSentryStatusClient.create_ocr_query.__get__(client.sentry_status)
    client.sentry_status.http.post = AsyncMock()
    await client.on_group_at_message_create(message(command))
    client.sentry_status.http.post.assert_not_awaited()
    assert "例如：" in client.qq.send_text.await_args.args[2]
    assert await recall_query(client.redis, "g1", "u1") is None
    client.queue.enqueue.assert_not_awaited()


@pytest.mark.parametrize("payload", [b"not json", b'{"mode":"filtered","name":""}',
                                    b'{"mode":"repeat"}', b'{"mode":"all_nodes","name":"Alice"}'])
async def test_invalid_history_does_not_fall_back_to_full_query(client, payload):
    await remember_query(client.redis, "g1", "u1", {"mode": "all_nodes"})
    key = (await client.redis.keys("qq:query:last:*"))[0]
    await client.redis.set(key, payload)
    await client.on_group_at_message_create(message("再查"))
    client.sentry_status.create_ocr_query.assert_not_awaited()
    client.sentry_status.query.assert_not_awaited()
