import httpx
import pytest
import respx

from eve_risk.sentry_status import (
    EveSentryStatusClient,
    SentryStatusError,
    format_monitoring_nodes,
    format_ocr_query,
    format_sentry_status,
    is_sentry_status_command,
    parse_sentry_query,
)


def _bootstrap() -> dict[str, object]:
    node = {
        "client_id": "detector-client:1",
        "client_type": "detector_client",
        "online": True,
        "details": {
            "monitoring": True,
            "targets": [
                {
                    "source_instance": "EVE - Hajimi6",
                    "character_name": "Hajimi6",
                    "system_name": "S-KSWL",
                    "monitoring": True,
                }
            ],
        },
    }
    safe_node = {
        "client_id": "detector-client:2",
        "client_type": "detector_client",
        "online": True,
        "details": {
            "monitoring": True,
            "targets": [
                {
                    "source_instance": "EVE - Scout",
                    "character_name": "Scout",
                    "system_name": "H-ADOC",
                    "monitoring": True,
                }
            ],
        },
    }
    alice = {
        "id": "ocr:alice",
        "active": True,
        "source": "eve-sentry-detector",
        "source_instance": "EVE - Hajimi6",
        "system_name": "S-KSWL",
        "name": "Alice",
        "first_seen_at": "2026-07-23T03:40:52+00:00",
        "metadata": {
            "corporation_name": "Glory Navy",
            "corporation_ticker": "G.N.V",
            "alliance_name": "Fraternity.",
            "alliance_ticker": "FRT",
        },
    }
    friendly = {**alice, "id": "ocr:friendly", "name": "Friendly Pilot"}
    return {
        "clients": {"heartbeats": [node, safe_node]},
        "map": {
            "systems": [
                {"system_name": "S-KSWL", "hostile_count": 2},
                {"system_name": "H-ADOC", "hostile_count": 0},
            ]
        },
        "active_intel": [alice, friendly],
        "alerts": [
            {
                "active_intel_id": "ocr:alice",
                "level": "critical",
                "score": 100,
            }
        ],
    }


def test_formats_online_nodes_and_only_alerted_hostiles() -> None:
    message = format_sentry_status(_bootstrap())

    assert message.startswith("### ⚠️ 当前节点敌情｜2 人")
    assert "| S-KSWL | 2 | 1 |" in message
    assert "| Alice | S-KSWL | [G.N.V] Glory Navy | [FRT] Fraternity. | — |" in message
    assert "Hajimi6" not in message
    assert "Scout" not in message
    assert "Friendly Pilot" not in message


def test_status_command_aliases_and_empty_snapshot() -> None:
    assert is_sentry_status_command("查询预警") is True
    assert is_sentry_status_command("查询") is True
    assert is_sentry_status_command("查") is True
    assert is_sentry_status_command("查预警") is True
    assert is_sentry_status_command("查询人员") is True
    assert is_sentry_status_command("查询军团") is True
    assert is_sentry_status_command("查询联盟") is True
    assert is_sentry_status_command("<@!bot> 预警详情") is True
    assert is_sentry_status_command("@机器人 敌对详情") is True
    assert is_sentry_status_command("/查询预警") is True
    assert is_sentry_status_command("<@!bot> /敌对详情") is True
    assert is_sentry_status_command("预警状态") is False
    assert "当前无活动敌情" in format_sentry_status({})


def test_parse_sentry_query_supports_person_and_affiliation_filters() -> None:
    assert parse_sentry_query("查询预警") == {"mode": "node_hostiles"}
    assert parse_sentry_query("查") == {"mode": "menu"}
    assert parse_sentry_query("查询 人员 Alice") == {"mode": "filtered", "name": "Alice"}
    assert parse_sentry_query("查预警 军团 Blue Corp") == {"mode": "filtered", "corporation": "Blue Corp"}
    assert parse_sentry_query("查询人员 Alice") == {"mode": "filtered", "name": "Alice"}
    assert parse_sentry_query("查询人员：Alice") == {"mode": "filtered", "name": "Alice"}
    assert parse_sentry_query("查询军团 Blue Corp") == {"mode": "filtered", "corporation": "Blue Corp"}
    assert parse_sentry_query("查询联盟 Example Alliance") == {
        "mode": "filtered",
        "alliance": "Example Alliance"
    }
    assert parse_sentry_query("查询人员") == {"mode": "filtered", "name": ""}
    assert parse_sentry_query("查询军团") == {"mode": "filtered", "corporation": ""}
    assert parse_sentry_query("查询联盟") == {"mode": "filtered", "alliance": ""}
    assert parse_sentry_query("@哨兵/查询人员 Hajimi1") == {"mode": "filtered", "name": "Hajimi1"}
    assert parse_sentry_query("<@!bot-user>/查询人员 Hajimi1") == {"mode": "filtered", "name": "Hajimi1"}
    assert parse_sentry_query("@哨兵/查询军团 Blue Corp") == {
        "mode": "filtered",
        "corporation": "Blue Corp"
    }
    assert parse_sentry_query("@哨兵/查询联盟 Example Alliance") == {
        "mode": "filtered",
        "alliance": "Example Alliance"
    }
    assert parse_sentry_query("@机器人 查询预警 人员 Alice") == {"mode": "filtered", "name": "Alice"}
    assert parse_sentry_query("/查询预警 军团 Blue Corp") == {"mode": "filtered", "corporation": "Blue Corp"}
    assert parse_sentry_query("查询预警 Alliance Name") == {"mode": "filtered", "name": "Alliance Name"}
    assert parse_sentry_query("查询星系 S-KSWL") == {"mode": "system_roster", "system_name": "S-KSWL"}
    assert parse_sentry_query("查询所有节点") == {"mode": "all_nodes"}
    assert parse_sentry_query("查询预警节点") == {"mode": "monitoring_nodes"}
    assert parse_sentry_query("预警状态") is None


def test_format_ocr_query_uses_only_names_from_this_snapshot() -> None:
    message = format_ocr_query(
        {
            "expected_clients": 1,
            "results": [
                {
                    "system_name": "S-KSWL",
                    "names": ["Alice", "Bob"],
                    "recognized": [
                        {
                            "name": "Alice",
                            "character_id": 123456,
                            "metadata": {
                                "corporation_name": "Blue Corp",
                                "alliance_name": "Example Alliance",
                            },
                        },
                        # A stale row must not affect this one-shot result.
                        {"name": "Old Pilot", "metadata": {}},
                    ],
                }
            ],
        }
    )

    assert message.startswith("### OCR 查询\n**节点**｜1/1")
    assert "S-KSWL｜识别 2 人" in message
    assert "| 人员 | 军团 | 联盟 | zKill |" in message
    assert (
        "| Alice | Blue Corp | Example Alliance | "
        "[查看](https://zkillboard.com/character/123456/) |"
    ) in message
    assert "| Bob | — | — | — |" in message
    assert "Old Pilot" not in message


@pytest.mark.asyncio
async def test_status_client_derives_bootstrap_endpoint() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(
            http,
            "http://sentry.test/api/v1/events",
            "eve_service_secret",
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.get("http://sentry.test/api/v1/bootstrap").mock(
                return_value=httpx.Response(200, json={"bootstrap": _bootstrap()})
            )
            result = await client.query()

    assert route.called
    assert route.calls[0].request.headers["Authorization"] == "Bearer eve_service_secret"
    assert "当前节点敌情｜2 人" in result


@pytest.mark.asyncio
async def test_status_client_sends_system_name_when_creating_ocr_query() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(
            http,
            "http://sentry.test/api/v1/events",
            "eve_service_secret",
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post("http://sentry.test/api/v1/ocr/query").mock(
                return_value=httpx.Response(
                    202,
                    json={
                        "query_id": "ocrq_1",
                        "requested_clients": ["node-1"],
                    },
                )
            )
            created = await client.create_ocr_query(
                {"mode": "system_roster", "system_name": "S-KSWL"}
            )

    assert created["query_id"] == "ocrq_1"
    assert route.calls[0].request.headers["Authorization"] == "Bearer eve_service_secret"
    assert route.calls[0].request.content == b'{"system_name":"S-KSWL"}'


@pytest.mark.asyncio
async def test_status_client_preserves_ocr_query_rejection_reason() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(
            http,
            "http://sentry.test/api/v1/events",
            "eve_service_secret",
        )
        with respx.mock(assert_all_called=True) as router:
            router.post("http://sentry.test/api/v1/ocr/query").mock(
                return_value=httpx.Response(
                    409,
                    json={"error": "星系 NCG-PW 当前没有在线监控节点"},
                )
            )
            with pytest.raises(
                SentryStatusError,
                match="星系 NCG-PW 当前没有在线监控节点",
            ):
                await client.create_ocr_query(
                    {"mode": "system_roster", "system_name": "NCG-PW"}
                )


@pytest.mark.asyncio
async def test_status_client_hides_ocr_query_server_error_details() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(
            http,
            "http://sentry.test/api/v1/events",
            "eve_service_secret",
        )
        with respx.mock(assert_all_called=True) as router:
            router.post("http://sentry.test/api/v1/ocr/query").mock(
                return_value=httpx.Response(
                    500,
                    json={"error": "internal database detail"},
                )
            )
            with pytest.raises(
                SentryStatusError,
                match="OCR 查询创建失败，请稍后重试",
            ):
                await client.create_ocr_query({"mode": "all_nodes"})


@pytest.mark.asyncio
async def test_fast_status_queries_do_not_create_ocr_jobs() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(http, "http://sentry.test/api/v1/events")
        with respx.mock(assert_all_called=True) as router:
            bootstrap_route = router.get("http://sentry.test/api/v1/bootstrap").mock(
                side_effect=[
                    httpx.Response(200, json={"bootstrap": _bootstrap()}),
                    httpx.Response(200, json={"bootstrap": _bootstrap()}),
                ]
            )
            await client.query({"mode": "node_hostiles"})
            await client.query({"mode": "monitoring_nodes"})

    assert bootstrap_route.call_count == 2


def test_formats_system_roster_with_names_only() -> None:
    message = format_ocr_query(
        {
            "expected_clients": 2,
            "system_name": "S-KSWL",
            "results": [
                {"names": ["Alice", "Bob"]},
                {"names": ["alice", "Carol"]},
            ],
        },
        {"mode": "system_roster", "system_name": "S-KSWL"},
    )

    assert message.startswith("### S-KSWL 当前名单｜3 人")
    assert "| 人员 |" in message
    assert "军团" not in message
    assert message.count("Alice") == 1


def test_formats_monitoring_nodes_without_ocr() -> None:
    bootstrap = {
        "monitoring_nodes": [
            {
                "client_id": "node-1",
                "system_name": "S-KSWL",
                "health_status": "online",
                "hostile_count": 2,
            }
        ]
    }

    message = format_monitoring_nodes(bootstrap)

    assert message.startswith("### 🛰️ 预警节点｜1")
    assert "| 监控节点 1 | 🟢 正常 | S-KSWL | 2 |" in message


@pytest.mark.asyncio
async def test_status_client_reports_missing_configuration() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(http, "")
        with pytest.raises(SentryStatusError, match="尚未配置"):
            await client.query()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"name": ""}, "人员名称"),
        ({"corporation": ""}, "军团名称"),
        ({"alliance": ""}, "联盟名称"),
    ],
)
async def test_targeted_query_requires_a_target(
    filters: dict[str, str], message: str
) -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(http, "http://sentry.test/api/v1/events")
        with pytest.raises(SentryStatusError, match=message):
            await client.query(filters, refresh=True)
