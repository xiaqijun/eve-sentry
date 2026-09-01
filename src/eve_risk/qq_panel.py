"""Idempotently configure the QQ group command panel."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx

from eve_risk.clients.base import request_with_retries
from eve_risk.config import get_settings

PANEL_SCOPE = "group"
PANEL_TARGET_TYPE = "all"
PANEL_REMARK = "EVE Sentry 群聊指令"
PANEL_ITEMS: tuple[dict[str, object], ...] = (
    {"type": "command", "name": "分析", "desc": "分析当前已确认敌对"},
    {"type": "command", "name": "查询预警", "desc": "查看当前敌对和监控节点"},
    {"type": "command", "name": "帮助", "desc": "查看机器人使用说明"},
    {"type": "command", "name": "开启预警", "desc": "开启本群主动预警"},
    {"type": "command", "name": "关闭预警", "desc": "关闭本群主动预警"},
    {"type": "command", "name": "预警状态", "desc": "查看本群预警状态"},
)


class QQPanelError(RuntimeError):
    """Raised when the QQ command panel cannot be configured."""


async def configure_group_panel(
    http: httpx.AsyncClient,
    *,
    app_id: str,
    app_secret: str,
    token_url: str,
    api_base_url: str,
    dry_run: bool = False,
) -> dict[str, object]:
    """Create or update the global group panel and return a safe summary."""
    token = await _fetch_access_token(http, app_id, app_secret, token_url)
    headers = {
        "Authorization": f"QQBot {token}",
        "X-Union-Appid": app_id,
        "Content-Type": "application/json",
    }
    base_url = api_base_url.rstrip("/")
    records = await _list_group_panels(http, base_url, headers)
    existing = next(
        (
            record
            for record in records
            if record.get("target_type") == PANEL_TARGET_TYPE
            and isinstance(record.get("panel"), Mapping)
            and record["panel"].get("remark") == PANEL_REMARK
        ),
        None,
    )
    desired_panel = {
        "items": [dict(item) for item in PANEL_ITEMS],
        "remark": PANEL_REMARK,
    }

    if existing is not None:
        panel_id = str(existing.get("panel_id") or "").strip()
        if not panel_id:
            raise QQPanelError("QQ group panel response omitted panel_id")
        current_panel = existing.get("panel")
        if _panel_content(current_panel) == _panel_content(desired_panel):
            return {"action": "unchanged", "panel_id": panel_id}
        if dry_run:
            return {"action": "update", "panel_id": panel_id, "dry_run": True}
        await _request_json(
            http,
            "PUT",
            f"{base_url}/v2/panels/{panel_id}",
            headers=headers,
            json_body={"panel": desired_panel},
        )
        return {"action": "updated", "panel_id": panel_id}

    if dry_run:
        return {"action": "create", "dry_run": True}
    payload = await _request_json(
        http,
        "POST",
        f"{base_url}/v2/panels",
        headers=headers,
        json_body={
            "scope": PANEL_SCOPE,
            "target_type": PANEL_TARGET_TYPE,
            "panel": desired_panel,
        },
    )
    panel_id = str(payload.get("panel_id") or "").strip()
    if not panel_id:
        raise QQPanelError("QQ group panel creation response omitted panel_id")
    return {"action": "created", "panel_id": panel_id}


async def _fetch_access_token(
    http: httpx.AsyncClient, app_id: str, app_secret: str, token_url: str
) -> str:
    payload = await _request_json(
        http,
        "POST",
        token_url,
        headers={"Content-Type": "application/json"},
        json_body={"appId": app_id, "clientSecret": app_secret},
    )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise QQPanelError("QQ access token response omitted access_token")
    return token


async def _list_group_panels(
    http: httpx.AsyncClient, base_url: str, headers: Mapping[str, str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params = {"scope": PANEL_SCOPE, "limit": "50"}
        if cursor:
            params["cursor"] = cursor
        payload = await _request_json(
            http,
            "GET",
            f"{base_url}/v2/panels",
            headers=headers,
            params=params,
        )
        raw_records = payload.get("records")
        if isinstance(raw_records, list):
            records.extend(item for item in raw_records if isinstance(item, dict))
        next_cursor = str(payload.get("next_cursor") or "").strip()
        if payload.get("is_end") is True or not next_cursor or next_cursor == cursor:
            return records
        cursor = next_cursor


async def _request_json(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    json_body: object | None = None,
    params: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, object] = {"headers": headers, "timeout": 15.0}
    if json_body is not None:
        kwargs["json"] = json_body
    if params is not None:
        kwargs["params"] = params
    try:
        response = await request_with_retries(http, method, url, **kwargs)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise QQPanelError(f"QQ panel {method} request failed") from exc
    if not isinstance(payload, dict):
        raise QQPanelError(f"QQ panel {method} response was not an object")
    return payload


def _panel_content(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    content = {
        "items": _panel_items(value.get("items")),
        "remark": str(value.get("remark") or ""),
    }
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _panel_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    for raw_item in value:
        if not isinstance(raw_item, Mapping):
            continue
        item: dict[str, object] = {
            "type": str(raw_item.get("type") or ""),
            "name": str(raw_item.get("name") or ""),
            "desc": str(raw_item.get("desc") or ""),
            "only_admin": bool(raw_item.get("only_admin", False)),
        }
        link = str(raw_item.get("link") or "")
        if link:
            item["link"] = link
        normalized.append(item)
    return normalized


async def _run(dry_run: bool) -> dict[str, object]:
    settings = get_settings()
    settings.require_qq()
    async with httpx.AsyncClient(headers={"Accept": "application/json"}) as http:
        return await configure_group_panel(
            http,
            app_id=settings.qq_app_id,
            app_secret=settings.qq_app_secret,
            token_url=settings.qq_token_url,
            api_base_url=settings.qq_api_base_url,
            dry_run=dry_run,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the QQ group command panel")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect the current panel without creating or updating it",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args.dry_run))
    except (QQPanelError, RuntimeError) as exc:
        parser.exit(1, f"QQ panel configuration failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
