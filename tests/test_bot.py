import botpy
import httpx
import pytest

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
