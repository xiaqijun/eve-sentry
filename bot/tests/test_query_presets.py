"""Preset management and callbacks must stay scoped to the originating group."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from test_query_shortcuts import client as client
from test_query_shortcuts import finish_queries, message

from eve_risk.bot import query_keyboard_content
from eve_risk.qq_panel import PANEL_ITEMS
from eve_risk.query_presets import (
    MAX_PRESETS,
    PresetError,
    QueryPresetStore,
    parse_preset_command,
)


@pytest.mark.parametrize("prefix", ["", "/", "@哨兵 ", "@哨兵/", "<@!bot-user>/"])
@pytest.mark.parametrize(("text", "kind", "target"), [
    ("预设 监控 Alice Example", "人员", "Alice Example"),
    ("预设，监控 Alice", "人员", "Alice"),
    ("预设,监控 角色：Alice", "人员", "Alice"),
    ("预设 监控 人员 Alice", "人员", "Alice"),
    ("预设 监控 军团 Blue Corp", "军团", "Blue Corp"),
    ("预设 监控 联盟: Example Alliance", "联盟", "Example Alliance"),
    ("预设 监控 星系 S-KSWL", "星系", "S-KSWL"),
])
def test_parse_typed_and_bare_presets(prefix, text, kind, target):
    command = parse_preset_command(prefix + text)
    assert (command.action, command.kind, command.target) == ("add", kind, target)


@pytest.mark.parametrize("text", ["预设 监控", "预设 监控 人员", "预设 监控 星系：",
                                  "预设 监控 A\nB", "预设 监控 " + "A" * 101,
                                  "预设 查询 Alice", "删除预设", "删除预设 ../../x"])
def test_parse_bad_presets_is_explicit_error(text):
    assert parse_preset_command(text).action == "error"


@pytest.mark.parametrize("text", ["查询", "查人 Alice", "上线监测 人员 Alice 30s", "预设是什么"])
def test_other_commands_are_untouched(text):
    assert parse_preset_command(text) is None


@pytest.mark.parametrize("prefix", ["", "/", "@哨兵 ", "<@!bot-id>/"])
def test_management_commands(prefix):
    assert parse_preset_command(prefix + "预设列表").action == "list"
    assert parse_preset_command(prefix + "预设").action == "list"
    assert parse_preset_command(prefix + "删除预设 ABCDEF123456").preset_id == "abcdef123456"


@pytest.fixture
async def redis():
    async with fakeredis.aioredis.FakeRedis() as connection:
        yield connection


async def test_storage_no_ttl_duplicate_ownership_and_group_isolation(redis):
    store = QueryPresetStore(redis, "g1")
    preset, created = await store.add("u1", "人员", "Alice")
    assert created
    duplicate, created = await store.add("u2", "人员", "alice")
    assert duplicate == preset and not created
    assert await redis.ttl(store.key) == -1
    assert await QueryPresetStore(redis, "g1").get(preset.preset_id) == preset
    assert await QueryPresetStore(redis, "g2").get(preset.preset_id) is None
    with pytest.raises(PresetError, match="自己"):
        await store.delete("u2", preset.preset_id)
    await store.delete("u1", preset.preset_id)
    assert await store.get(preset.preset_id) is None
    new, _ = await store.add("u1", "人员", "Alice")
    assert new.preset_id != preset.preset_id


async def test_concurrent_adds_enforce_limit_and_deduplicate(redis):
    store = QueryPresetStore(redis, "g1")
    results = await asyncio.gather(*(store.add("u1", "人员", "Alice") for _ in range(5)))
    assert sum(created for _, created in results) == 1
    results = await asyncio.gather(
        *(store.add("u1", "人员", f"Other {index}") for index in range(12)),
        return_exceptions=True,
    )
    assert len(await store.list()) == MAX_PRESETS
    assert all(isinstance(result, (tuple, PresetError)) for result in results)
    buttons = [button for row in query_keyboard_content(await store.list())["rows"]
               for button in row["buttons"]]
    assert len(buttons) == 10
    assert all(button["action"]["type"] == 1 for button in buttons)


@pytest.mark.parametrize("raw", [b"not json", b"{}", b"[]",
                                  b'{"kind":[],"target":"Alice","owner":"x"}'])
async def test_corrupt_records_fail_closed(redis, raw):
    store = QueryPresetStore(redis, "g1")
    await redis.hset(store.key, "abcdef123456", raw)
    with pytest.raises(PresetError):
        await store.get("abcdef123456")


def interaction(data, group="g1", event_id="i1"):
    return SimpleNamespace(id=event_id, group_openid=group,
                           data=SimpleNamespace(resolved=SimpleNamespace(button_data=data)))


@pytest.fixture
def callback_client(client):
    client.qq.send_proactive_text = AsyncMock()
    client.api.on_interaction_result = AsyncMock()
    return client


@pytest.mark.parametrize(("target", "query"), [
    ("Alice", {"mode": "filtered", "name": "Alice"}),
    ("军团 Blue Corp", {"mode": "filtered", "corporation": "Blue Corp"}),
    ("联盟 Example", {"mode": "filtered", "alliance": "Example"}),
    ("星系 S-KSWL", {"mode": "system_roster", "system_name": "S-KSWL"}),
])
async def test_add_menu_click_fresh_query_and_callback_dedup(callback_client, target, query):
    bot = callback_client
    await bot.on_group_at_message_create(message("预设 监控 " + target))
    bot.sentry_status.create_ocr_query.assert_not_awaited()
    bot.queue.enqueue.assert_not_awaited()
    menu = bot.qq.send_markdown.await_args
    assert "已添加" in menu.args[2]
    buttons = [button for row in menu.kwargs["keyboard_content"]["rows"] for button in row["buttons"]]
    assert len(buttons) == 4
    callback = buttons[-1]["action"]["data"]
    assert callback.startswith("preset:") and target not in callback
    await bot.on_interaction_create(interaction(callback))
    await finish_queries(bot)
    await bot.on_interaction_create(interaction(callback))
    bot.sentry_status.create_ocr_query.assert_awaited_once_with(query)
    await bot.on_interaction_create(interaction(callback, event_id="i2"))
    await finish_queries(bot)
    assert bot.sentry_status.create_ocr_query.await_count == 2


async def test_invalid_foreign_deleted_and_arbitrary_callbacks_never_query(callback_client):
    bot = callback_client
    store = QueryPresetStore(bot.redis, "g1")
    preset, _ = await store.add("u1", "人员", "Alice")
    callback = f"preset:{preset.preset_id}"
    await bot.on_interaction_create(interaction(callback, group="g2"))
    assert "已失效" in bot.qq.send_proactive_text.await_args.args[1]
    await store.delete("u1", preset.preset_id)
    await bot.on_interaction_create(interaction(callback, event_id="i2"))
    await bot.on_interaction_create(interaction("preset:invalid", event_id="i3"))
    await bot.on_interaction_create(interaction("查人 Alice", event_id="i4"))
    bot.sentry_status.create_ocr_query.assert_not_awaited()
    bot.sentry_status.query.assert_not_awaited()


async def test_management_validation_does_not_dispatch_analysis(callback_client):
    bot = callback_client
    await bot.on_group_at_message_create(message("预设 监控"))
    assert "例如" in bot.qq.send_text.await_args.args[2]
    await bot.on_group_at_message_create(message("预设 监控 Alice", "m2"))
    preset = (await QueryPresetStore(bot.redis, "g1").list())[0]
    await bot.on_group_at_message_create(message("预设列表", "m3"))
    assert preset.preset_id in bot.qq.send_markdown.await_args.args[2]
    await bot.on_group_at_message_create(message("删除预设 " + preset.preset_id, "m4", member="u2"))
    assert "自己" in bot.qq.send_text.await_args.args[2]
    await bot.on_group_at_message_create(message("删除预设 " + preset.preset_id, "m5"))
    assert "已删除" in bot.qq.send_markdown.await_args.args[2]
    bot.queue.enqueue.assert_not_awaited()
    bot.sentry_status.create_ocr_query.assert_not_awaited()


async def test_preset_callback_obeys_group_ocr_lock(callback_client):
    bot = callback_client
    preset, _ = await QueryPresetStore(bot.redis, "g1").add("u1", "人员", "Alice")
    import hashlib
    key = "qq:ocr-query:group:" + hashlib.sha256(b"g1").hexdigest()[:16]
    await bot.redis.set(key, "other", ex=45)
    await bot.on_interaction_create(interaction(f"preset:{preset.preset_id}"))
    assert "正在进行" in bot.qq.send_proactive_text.await_args.args[1]
    bot.sentry_status.create_ocr_query.assert_not_awaited()


async def test_storage_failure_shows_error_and_keeps_basic_menu(callback_client, monkeypatch):
    bot = callback_client
    monkeypatch.setattr(QueryPresetStore, "list", AsyncMock(side_effect=RuntimeError("down")))
    await bot.on_group_at_message_create(message("查询"))
    args = bot.qq.send_markdown.await_args
    assert args.kwargs["keyboard_content"] == query_keyboard_content()
    assert "预设暂不可用" in args.args[2]
    monkeypatch.setattr(QueryPresetStore, "add", AsyncMock(side_effect=RuntimeError("down")))
    await bot.on_group_at_message_create(message("预设 监控 Alice", "m2"))
    assert "暂不可用" in bot.qq.send_text.await_args.args[2]


def test_preset_management_present_in_command_panel():
    assert "预设" in {item["name"] for item in PANEL_ITEMS}
