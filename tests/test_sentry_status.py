import httpx
import pytest
import respx

from eve_risk.sentry_status import (
    EveSentryStatusClient,
    SentryStatusError,
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

    assert message.startswith("预警节点｜在线 2｜敌对 1 人")
    assert "🔴 S-KSWL｜敌 1｜监控节点 1" in message
    assert "Alice｜严重 100｜发现 07-23 11:40:52" in message
    assert "军团｜[G.N.V] Glory Navy" in message
    assert "联盟｜[FRT] Fraternity." in message
    assert "🟢 H-ADOC｜敌 0｜监控节点 2" in message
    assert "Hajimi6" not in message
    assert "Scout" not in message
    assert "Friendly Pilot" not in message


def test_status_command_aliases_and_empty_snapshot() -> None:
    assert is_sentry_status_command("查询预警") is True
    assert is_sentry_status_command("<@!bot> 预警详情") is True
    assert is_sentry_status_command("@机器人 敌对详情") is True
    assert is_sentry_status_command("/查询预警") is True
    assert is_sentry_status_command("<@!bot> /敌对详情") is True
    assert is_sentry_status_command("预警状态") is False
    assert format_sentry_status({}) == "预警节点｜当前无在线监控节点"


def test_parse_sentry_query_supports_person_and_affiliation_filters() -> None:
    assert parse_sentry_query("查询预警") == {}
    assert parse_sentry_query("@机器人 查询预警 人员 Alice") == {"name": "Alice"}
    assert parse_sentry_query("/查询预警 军团 Blue Corp") == {"corporation": "Blue Corp"}
    assert parse_sentry_query("查询预警 Alliance Name") == {"name": "Alliance Name"}
    assert parse_sentry_query("预警状态") is None


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
    assert "敌对 1 人" in result


@pytest.mark.asyncio
async def test_status_client_reports_missing_configuration() -> None:
    async with httpx.AsyncClient() as http:
        client = EveSentryStatusClient(http, "")
        with pytest.raises(SentryStatusError, match="尚未配置"):
            await client.query()
