import json

import httpx
import pytest
import respx

from eve_risk.qq_panel import PANEL_ITEMS, PANEL_REMARK, configure_group_panel


@pytest.mark.asyncio
async def test_configure_group_panel_creates_global_panel() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            router.post("https://bots.qq.com/app/getAppAccessToken").mock(
                return_value=httpx.Response(200, json={"access_token": "token"})
            )
            router.get("https://api.sgroup.qq.com/v2/panels").mock(
                return_value=httpx.Response(200, json={"records": [], "is_end": True})
            )
            create = router.post("https://api.sgroup.qq.com/v2/panels").mock(
                return_value=httpx.Response(200, json={"panel_id": "panel-1"})
            )

            result = await configure_group_panel(
                http,
                app_id="appid",
                app_secret="secret",
                token_url="https://bots.qq.com/app/getAppAccessToken",
                api_base_url="https://api.sgroup.qq.com",
            )

            assert result == {"action": "created", "panel_id": "panel-1"}
            body = json.loads(create.calls[0].request.content)
            assert body["scope"] == "group"
            assert body["target_type"] == "all"
            assert body["panel"]["remark"] == PANEL_REMARK
            assert body["panel"]["items"] == list(PANEL_ITEMS)


@pytest.mark.asyncio
async def test_configure_group_panel_updates_only_when_content_changes() -> None:
    current = {
        "panel_id": "panel-1",
        "scope": "group",
        "target_type": "all",
        "panel": {"items": [{"type": "command", "name": "旧指令"}], "remark": PANEL_REMARK},
    }
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            router.post("https://bots.qq.com/app/getAppAccessToken").mock(
                return_value=httpx.Response(200, json={"access_token": "token"})
            )
            router.get("https://api.sgroup.qq.com/v2/panels").mock(
                return_value=httpx.Response(200, json={"records": [current], "is_end": True})
            )
            update = router.put("https://api.sgroup.qq.com/v2/panels/panel-1").mock(
                return_value=httpx.Response(200, json={"version": 2})
            )

            result = await configure_group_panel(
                http,
                app_id="appid",
                app_secret="secret",
                token_url="https://bots.qq.com/app/getAppAccessToken",
                api_base_url="https://api.sgroup.qq.com",
            )

            assert result == {"action": "updated", "panel_id": "panel-1"}
            body = json.loads(update.calls[0].request.content)
            assert body == {
                "panel": {"items": list(PANEL_ITEMS), "remark": PANEL_REMARK}
            }


@pytest.mark.asyncio
async def test_configure_group_panel_is_unchanged_when_content_matches() -> None:
    current = {
        "panel_id": "panel-1",
        "scope": "group",
        "target_type": "all",
        "panel": {
            "items": [{**item, "only_admin": False} for item in PANEL_ITEMS],
            "remark": PANEL_REMARK,
        },
    }
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            router.post("https://bots.qq.com/app/getAppAccessToken").mock(
                return_value=httpx.Response(200, json={"access_token": "token"})
            )
            router.get("https://api.sgroup.qq.com/v2/panels").mock(
                return_value=httpx.Response(200, json={"records": [current], "is_end": True})
            )

            result = await configure_group_panel(
                http,
                app_id="appid",
                app_secret="secret",
                token_url="https://bots.qq.com/app/getAppAccessToken",
                api_base_url="https://api.sgroup.qq.com",
            )

            assert result == {"action": "unchanged", "panel_id": "panel-1"}
