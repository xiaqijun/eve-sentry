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
        content = "分析 Alice，Bob"
        author = Author()

    try:
        await client.on_group_at_message_create(Message())

        assert client.queue.enqueue.await_count == 2
        requests = [call.args[0] for call in client.queue.enqueue.await_args_list]
        assert [request.character_names for request in requests] == [["Alice"], ["Bob"]]
        assert len({request.request_id for request in requests}) == 2
        assert [request.reply_seq for request in requests] == [1, 2]
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
        client.qq.send_text.assert_awaited_once_with(
            "group-1",
            "status-message-1",
            "预警节点｜在线 1｜敌对 0 人\n🟢 S-KSWL｜敌 0｜监控节点 1",
            msg_seq=1,
        )
    finally:
        await client.http_client.aclose()
        await redis.aclose()
        await original_redis.aclose()
