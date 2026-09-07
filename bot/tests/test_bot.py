import asyncio
import hashlib
from unittest.mock import AsyncMock

import botpy
import fakeredis.aioredis
import httpx
import pytest

from eve_risk.admission import AdmissionResult
from eve_risk.bot import RiskBotClient


@pytest.mark.asyncio
async def test_custom_http_client_does_not_replace_botpy_login_client() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    try:
        assert hasattr(client.http, "login")
        assert isinstance(client.http_client, httpx.AsyncClient)
    finally:
        await client.http_client.aclose()
        await client.redis.aclose()


@pytest.mark.asyncio
async def test_group_can_enable_and_disable_proactive_alerts() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.alert_relay.redis = redis
    client.alert_relay.events_url = "http://sentry.test/api/v1/events"
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "message-1"
        group_openid = "group-1"
        content = "开启预警"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())
        assert await client.alert_relay.is_subscribed("group-1") is True
        client.qq.send_text.assert_awaited_once_with(
            "group-1",
            "message-1",
            "已开启 EVE Sentry 主动预警，新敌对告警会推送到本群。",
            msg_seq=1,
        )

        Message.id = "message-2"
        Message.content = "关闭预警"
        await client.on_group_at_message_create(Message())
        assert await client.alert_relay.is_subscribed("group-1") is False
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_analysis_query_is_enqueued_without_intermediate_text_reply() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.admission.admit = AsyncMock(return_value=AdmissionResult.OK)
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "analysis-message-1"
        group_openid = "group-1"
        content = "分析 MP5K"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        client.queue.enqueue.assert_awaited_once()
        request = client.queue.enqueue.await_args.args[0]
        assert request.character_names == ["MP5K"]
        client.qq.send_text.assert_not_awaited()
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_multi_character_analysis_enqueues_one_request_per_character() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.admission.admit = AsyncMock(return_value=AdmissionResult.OK)
    client.admission.admit_batch = AsyncMock(return_value=AdmissionResult.OK)
    client.queue.enqueue = AsyncMock()

    class Author:
        member_openid = "member-1"

    class Message:
        id = "analysis-batch-message-1"
        group_openid = "group-1"
        content = "分析 Alice，Bob，Carol，Dave"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        assert client.queue.enqueue.await_count == 4
        requests = [call.args[0] for call in client.queue.enqueue.await_args_list]
        assert [request.character_names for request in requests] == [
            ["Alice"],
            ["Bob"],
            ["Carol"],
            ["Dave"],
        ]
        assert len({request.request_id for request in requests}) == 4
        assert [request.reply_seq for request in requests] == [1, 2, 3, 4]
        assert all(request.admission_batch_id == "message:analysis-batch-message-1" for request in requests)
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_bare_analysis_uses_current_hostile_personnel() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.alert_relay.current_analysis_names = AsyncMock(
        return_value=["Alice", "Bob"]
    )
    client.admission.admit_batch = AsyncMock(return_value=AdmissionResult.OK)
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "current-hostiles-message-1"
        group_openid = "group-1"
        content = "分析"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        client.alert_relay.current_analysis_names.assert_awaited_once_with(
            client.settings.max_characters
        )
        requests = [call.args[0] for call in client.queue.enqueue.await_args_list]
        assert [request.character_names for request in requests] == [["Alice"], ["Bob"]]
        assert [request.reply_seq for request in requests] == [1, 2]
        client.qq.send_text.assert_not_awaited()
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_bare_analysis_without_current_personnel_does_not_enqueue() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.alert_relay.current_analysis_names = AsyncMock(return_value=[])
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "current-hostiles-empty-message-1"
        group_openid = "group-1"
        content = "分析"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        client.queue.enqueue.assert_not_awaited()
        client.qq.send_text.assert_awaited_once_with(
            "group-1",
            "current-hostiles-empty-message-1",
            "当前没有已确认的敌对人员可供分析。",
            msg_seq=1,
        )
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_non_analysis_mention_does_not_enqueue_analysis() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "ordinary-message-1"
        group_openid = "group-1"
        content = "Alice"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        client.queue.enqueue.assert_not_awaited()
        client.qq.send_text.assert_awaited_once()
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_sentry_status_query_replies_without_analysis_queue() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})
    client.qq.send_proactive_markdown = AsyncMock(return_value={"id": "markdown"})
    client.sentry_status.query = AsyncMock(
        return_value="预警节点｜在线 1｜敌对 0 人\n🟢 S-KSWL｜敌 0｜监控节点 1"
    )

    class Author:
        member_openid = "member-1"

    class Message:
        id = "status-message-1"
        group_openid = "group-1"
        content = "查询预警"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        client.queue.enqueue.assert_not_awaited()
        client.qq.send_proactive_markdown.assert_awaited_once_with(
            "group-1",
            "预警节点｜在线 1｜敌对 0 人\n🟢 S-KSWL｜敌 0｜监控节点 1",
        )
        client.qq.send_text.assert_not_awaited()
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_sentry_status_query_falls_back_when_markdown_delivery_fails() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.queue.enqueue = AsyncMock()
    client.qq.send_text = AsyncMock(return_value={"id": "reply"})
    client.qq.send_proactive_markdown = AsyncMock(side_effect=RuntimeError("unsupported"))
    client.sentry_status.query = AsyncMock(return_value="| 人员 | 军团 |\n| --- | --- |")

    class Author:
        member_openid = "member-1"

    class Message:
        id = "status-message-fallback"
        group_openid = "group-1"
        content = "查询预警"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        client.qq.send_proactive_markdown.assert_awaited_once()
        client.qq.send_text.assert_awaited_once_with(
            "group-1",
            "status-message-fallback",
            "| 人员 | 军团 |\n| --- | --- |",
            msg_seq=1,
        )
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_query_command_opens_markdown_keyboard_menu() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.settings.qq_query_keyboard_id = "query-keyboard"
    client.qq.send_markdown = AsyncMock(return_value={"id": "menu"})

    class Author:
        member_openid = "member-1"

    class Message:
        id = "query-menu-1"
        group_openid = "group-1"
        content = "查询"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())
        client.qq.send_markdown.assert_awaited_once()
        assert client.qq.send_markdown.await_args.kwargs["keyboard_id"] == "query-keyboard"
        assert "哨兵查询" in client.qq.send_markdown.await_args.args[2]
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()


@pytest.mark.asyncio
async def test_active_ocr_query_acknowledges_then_sends_result() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.qq.send_text = AsyncMock(return_value={"id": "ack"})
    client.qq.send_proactive_markdown = AsyncMock(return_value={"id": "result"})
    client.sentry_status.create_ocr_query = AsyncMock(
        return_value={"query_id": "ocrq_1", "requested_clients": ["node-1"]}
    )
    client.sentry_status.wait_ocr_query_payload = AsyncMock(
        return_value={
            "expected_clients": 1,
            "received_clients": 1,
            "results": [{"system_name": "S-KSWL", "names": ["Alice"]}],
        }
    )

    try:
        await client._start_sentry_ocr_query(
            {"mode": "system_roster", "system_name": "S-KSWL"},
            group_openid="group-1",
            msg_id="message-1",
        )
        tasks = list(client.query_tasks)
        await asyncio.gather(*tasks)
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()

    client.qq.send_text.assert_awaited_once_with(
        "group-1",
        "message-1",
        "OCR 查询任务已下发｜目标节点 1",
        msg_seq=1,
    )
    client.qq.send_proactive_markdown.assert_awaited_once()
    assert "S-KSWL 当前名单" in client.qq.send_proactive_markdown.await_args.args[1]


@pytest.mark.asyncio
async def test_active_ocr_query_rejects_a_second_query_in_the_same_group() -> None:
    client = RiskBotClient(intents=botpy.Intents(public_messages=True), bot_log=False)
    original_redis = client.redis
    redis = fakeredis.aioredis.FakeRedis()
    client.redis = redis
    client.qq.send_text = AsyncMock(return_value={"id": "busy"})
    client.sentry_status.create_ocr_query = AsyncMock()
    group_openid = "group-1"
    group_hash = hashlib.sha256(group_openid.encode("utf-8")).hexdigest()[:16]
    await redis.set(f"qq:ocr-query:group:{group_hash}", "existing", ex=45)

    try:
        await client._start_sentry_ocr_query(
            {"mode": "all_nodes"},
            group_openid=group_openid,
            msg_id="message-2",
        )
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()

    client.sentry_status.create_ocr_query.assert_not_awaited()
    client.qq.send_text.assert_awaited_once_with(
        group_openid,
        "message-2",
        "本群已有 OCR 查询正在进行，请等待当前结果。",
        msg_seq=1,
    )
