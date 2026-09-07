from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from eve_risk.parser import normalize_command_content

logger = logging.getLogger(__name__)

# Keep the full command names for readability while also accepting short
# aliases that are convenient to type in a busy group chat.
QUERY_COMMANDS = {
    "查询预警",
    "预警详情",
    "敌对详情",
    "节点敌对",
    "查预警",
    "查询",
    "查",
    "查询人员",
    "查询军团",
    "查询联盟",
    "查询星系",
    "查询节点敌情",
    "查询所有节点",
    "查询预警节点",
}
TARGETED_QUERY_COMMANDS = {
    "查询人员": ("name", "人员名称"),
    "查询军团": ("corporation", "军团名称"),
    "查询联盟": ("alliance", "联盟名称"),
}
DETECTOR_SOURCES = {"eve-sentry-detector", "local_ocr", "ocr"}
MAX_NODES = 20
MAX_HOSTILES = 30
SHANGHAI = timezone(timedelta(hours=8))


class SentryStatusError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlertNode:
    system_name: str
    source_instance: str
    label: str

    @property
    def key(self) -> tuple[str, str]:
        return self.system_name.casefold(), self.source_instance.casefold()


class EveSentryStatusClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        events_url: str,
        api_key: str = "",
    ) -> None:
        self.http = http
        self.bootstrap_url = _bootstrap_url(events_url)
        self.api_key = api_key.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.bootstrap_url)

    async def query(
        self,
        request: dict[str, str] | None = None,
        *,
        refresh: bool = False,
    ) -> str:
        if not self.enabled:
            raise SentryStatusError("预警服务尚未配置，请联系机器人管理员。")
        query = dict(request or {})
        mode = str(query.get("mode") or ("filtered" if refresh else "node_hostiles"))
        if mode == "menu":
            return format_query_menu()
        if mode in {"system_roster", "filtered", "all_nodes"}:
            self._validate_ocr_query(query)
            return await self._query_ocr(query)
        bootstrap = await self.bootstrap()
        if mode == "monitoring_nodes":
            return format_monitoring_nodes(bootstrap)
        return format_sentry_status(bootstrap)

    async def bootstrap(self) -> dict[str, Any]:
        try:
            response = await self.http.get(
                self.bootstrap_url,
                headers=_sentry_headers(self.api_key),
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.exception("EVE Sentry status query failed")
            raise SentryStatusError("预警服务连接异常，请稍后重试。") from None

        bootstrap = payload.get("bootstrap") if isinstance(payload, dict) else None
        if not isinstance(bootstrap, dict):
            raise SentryStatusError("预警服务返回数据异常，请稍后重试。")
        return bootstrap

    def _validate_ocr_query(self, query: dict[str, str]) -> None:
        mode = str(query.get("mode") or "filtered")
        if mode == "system_roster" and not str(query.get("system_name") or "").strip():
            raise SentryStatusError("请指定要查询的星系名称。")
        for key, label in (
            ("name", "人员名称"),
            ("corporation", "军团名称"),
            ("alliance", "联盟名称"),
        ):
            if key in query and not str(query[key]).strip():
                raise SentryStatusError(f"请指定要查询的{label}。")

    async def create_ocr_query(self, query: dict[str, str]) -> dict[str, Any]:
        self._validate_ocr_query(query)
        query_url = _ocr_query_url(self.bootstrap_url)
        payload = {
            key: str(query.get(key) or "").strip()
            for key in ("name", "corporation", "alliance", "system_name")
            if str(query.get(key) or "").strip()
        }
        try:
            response = await self.http.post(
                query_url,
                headers=_sentry_headers(self.api_key),
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            created = response.json()
            query_id = str(created.get("query_id") or "").strip()
            if not query_id:
                raise SentryStatusError("预警服务未返回 OCR 查询编号。")
            return created
        except SentryStatusError:
            raise
        except Exception:
            logger.exception("EVE Sentry OCR query creation failed")
            raise SentryStatusError("OCR 查询创建失败，请稍后重试。") from None

    async def wait_ocr_query_payload(
        self,
        created: dict[str, Any],
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        query_id = str(created.get("query_id") or "").strip()
        if not query_id:
            raise SentryStatusError("预警服务未返回 OCR 查询编号。")
        status_url = f"{_ocr_query_url(self.bootstrap_url)}/{query_id}"
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        latest: dict[str, Any] = {}
        try:
            while asyncio.get_running_loop().time() < deadline:
                response = await self.http.get(
                    status_url,
                    headers=_sentry_headers(self.api_key),
                    timeout=10.0,
                )
                response.raise_for_status()
                payload = response.json()
                latest = payload if isinstance(payload, dict) else {}
                if str(latest.get("status") or "") in {"completed", "timed_out"}:
                    return latest
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("EVE Sentry OCR query polling failed")
            raise SentryStatusError("OCR 查询失败或客户端未响应，请稍后重试。") from None
        if latest.get("results"):
            latest["soft_timed_out"] = True
            return latest
        raise SentryStatusError("OCR 查询超时，当前没有收到客户端回传。")

    async def _query_ocr(self, query: dict[str, str]) -> str:
        created = await self.create_ocr_query(query)
        status = await self.wait_ocr_query_payload(created)
        return format_ocr_query(status, query)


def is_sentry_status_command(content: str) -> bool:
    return parse_sentry_query(content) is not None


def parse_sentry_query(content: str) -> dict[str, str] | None:
    normalized = normalize_command_content(content)
    if normalized in {"查询", "查"}:
        return {"mode": "menu"}
    if normalized in {"查询节点敌情", "查询预警", "查预警", "预警详情", "敌对详情", "节点敌对"}:
        return {"mode": "node_hostiles"}
    if normalized == "查询所有节点":
        return {"mode": "all_nodes"}
    if normalized == "查询预警节点":
        return {"mode": "monitoring_nodes"}
    if normalized == "查询星系":
        return {"mode": "system_roster", "system_name": ""}
    system_match = re.match(
        r"^查询星系(?:\s+|[：:]\s*)(.+)$",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if system_match:
        return {
            "mode": "system_roster",
            "system_name": str(system_match.group(1) or "").strip(),
        }
    generic_target = re.match(
        r"^(?:查询|查)\s+(人员|角色|军团|联盟)\s*(.+)$",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if generic_target:
        key = {
            "人员": "name",
            "角色": "name",
            "军团": "corporation",
            "联盟": "alliance",
        }[str(generic_target.group(1))]
        return {
            "mode": "filtered",
            key: str(generic_target.group(2) or "").strip(),
        }
    for command, (key, _label) in TARGETED_QUERY_COMMANDS.items():
        if normalized == command:
            return {"mode": "filtered", key: ""}
        match = re.match(
            rf"^{re.escape(command)}(?:\s+|[：:]\s*)(.+)$",
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return {
                "mode": "filtered",
                key: str(match.group(1) or "").strip(),
            }
    match = re.match(
        r"^(?:查询预警|查预警)\s+(?:(人员|角色|军团|联盟)\s*)?(.+)$",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    key = {
        "人员": "name",
        "角色": "name",
        "军团": "corporation",
        "联盟": "alliance",
    }.get(str(match.group(1) or ""), "name")
    return {"mode": "filtered", key: str(match.group(2) or "").strip()}


def format_query_menu() -> str:
    return "\n".join(
        (
            "### 🔎 哨兵查询",
            "请选择查询方式：",
            "`查询星系 名称`｜`查询节点敌情`｜`查询人员 名称`",
            "`查询军团 名称`｜`查询所有节点`｜`查询联盟 名称`",
            "`查询预警节点`｜`上线监测 人员/军团/联盟 名称 间隔`",
        )
    )


def format_ocr_query(payload: dict[str, Any], query: dict[str, str] | None = None) -> str:
    results = payload.get("results")
    results = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
    request = dict(query or {})
    mode = str(request.get("mode") or "filtered")
    if mode == "system_roster":
        return _format_system_roster(payload, results, request)
    filters = {
        key: str(request.get(key) or "").strip()
        for key in ("name", "corporation", "alliance")
        if str(request.get(key) or "").strip()
    }
    lines = [
        "### 所有节点当前名单" if mode == "all_nodes" else "### OCR 查询",
        f"**节点**｜{len(results)}/{int(payload.get('expected_clients') or len(results))}",
    ]
    if payload.get("soft_timed_out"):
        lines.append("**状态**｜部分节点未在 15 秒内返回")
    rendered_systems = 0
    for result_index, result in enumerate(results, start=1):
        system = str(result.get("system_name") or "未知星系").strip()
        recognized = result.get("recognized")
        recognized = [item for item in recognized if isinstance(item, dict)] if isinstance(recognized, list) else []
        selected = [item for item in recognized if _query_item_matches(item, filters)]
        raw_names = [str(name).strip() for name in result.get("names", []) if str(name).strip()]
        if not selected and filters:
            continue
        if filters:
            displayed_items = selected[:MAX_HOSTILES]
            lines.extend(
                (
                    f"\n#### {system}｜识别 {len(selected)} 人",
                    "| 人员 | 军团 | 联盟 | zKill |",
                    "| --- | --- | --- | --- |",
                )
            )
            for item in displayed_items:
                lines.append(_ocr_query_table_row(_hostile_name(item), item))
            rendered_systems += 1
            continue

        # The query response is authoritative for this one OCR pass. Use its
        # raw names as the count/list and only enrich names that were resolved
        # by the server; do not fall back to an older active-intel snapshot.
        recognized_by_name = {
            str(item.get("name") or "").strip().casefold(): item
            for item in recognized
            if str(item.get("name") or "").strip().casefold()
        }
        section_title = (
            f"\n#### 监控节点 {result_index}｜{system}｜识别 {len(raw_names)} 人"
            if mode == "all_nodes"
            else f"\n#### {system}｜识别 {len(raw_names)} 人"
        )
        lines.extend(
            (
                section_title,
                "| 人员 | 军团 | 联盟 | zKill |",
                "| --- | --- | --- | --- |",
            )
        )
        for name in raw_names[:MAX_HOSTILES]:
            item = recognized_by_name.get(name.casefold())
            lines.append(_ocr_query_table_row(name, item))
        if not raw_names:
            lines.append("| 暂无人员 | — | — | — |")
        rendered_systems += 1
    if not rendered_systems:
        return "OCR 查询｜没有收到符合条件的人员名单。"
    return "\n".join(lines)


def _format_system_roster(
    payload: dict[str, Any],
    results: list[dict[str, Any]],
    query: dict[str, str],
) -> str:
    system_name = str(
        query.get("system_name") or payload.get("system_name") or "未知星系"
    ).strip() or "未知星系"
    names: dict[str, str] = {}
    for result in results:
        for raw_name in result.get("names", []):
            name = str(raw_name or "").strip()
            if name:
                names.setdefault(name.casefold(), name)
    expected = int(payload.get("expected_clients") or len(results))
    lines = [
        f"### {system_name} 当前名单｜{len(names)} 人",
        f"**响应节点**｜{len(results)}/{expected}",
        "| 人员 |",
        "| --- |",
    ]
    if names:
        lines.extend(f"| {_escape_markdown_table_cell(name)} |" for name in sorted(names.values(), key=str.casefold))
    else:
        lines.append("| 暂无人员 |")
    return "\n".join(lines)


def _ocr_query_table_row(name: str, item: dict[str, Any] | None) -> str:
    resolved = item if isinstance(item, dict) else {}
    metadata = resolved.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    corporation = _affiliation(metadata, "corporation")
    alliance = _affiliation(metadata, "alliance")
    character_id = str(
        resolved.get("character_id") or metadata.get("character_id") or ""
    ).strip()
    zkill = (
        f"[查看](https://zkillboard.com/character/{character_id}/)"
        if character_id.isdigit()
        else "—"
    )
    return "| " + " | ".join(
        (
            _escape_markdown_table_cell(name or "未知人员"),
            _escape_markdown_table_cell(corporation),
            _escape_markdown_table_cell(alliance),
            zkill,
        )
    ) + " |"


def _escape_markdown_table_cell(value: object) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ").strip() or "—"


def _query_item_matches(item: dict[str, Any], filters: dict[str, str]) -> bool:
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    name_filter = str(filters.get("name") or "").strip().casefold()
    if name_filter and name_filter not in str(item.get("name") or "").casefold():
        return False
    for key, field in (("corporation", "corporation"), ("alliance", "alliance")):
        value = str(filters.get(key) or "").strip().casefold()
        if value:
            affiliation = _affiliation(metadata, field).casefold()
            if value not in affiliation:
                return False
    return True


def format_sentry_status(bootstrap: dict[str, Any]) -> str:
    hostiles = _current_hostiles(bootstrap)
    hostile_counts, system_names = _hostile_counts_by_system(bootstrap)
    hostiles_by_system: dict[str, list[dict[str, Any]]] = {}
    for item in hostiles:
        system_name = _system_name(item)
        system_key = system_name.casefold()
        system_names.setdefault(system_key, system_name)
        hostiles_by_system.setdefault(system_key, []).append(item)
    for system_key, items in hostiles_by_system.items():
        hostile_counts.setdefault(system_key, len(items))
    total_hostiles = sum(hostile_counts.values())
    lines = [
        f"### ⚠️ 当前节点敌情｜{total_hostiles} 人",
        "| 星系 | 当前敌对 | 已识别 |",
        "| --- | ---: | ---: |",
    ]
    active_systems = sorted(
        {
            system_key
            for system_key, count in hostile_counts.items()
            if count > 0
        }
        | set(hostiles_by_system),
        key=lambda key: system_names.get(key, key).casefold(),
    )
    if not active_systems:
        lines.append("| 当前无活动敌情 | 0 | 0 |")
        return "\n".join(lines)
    for system_key in active_systems:
        items = hostiles_by_system.get(system_key, [])
        system_name = system_names.get(system_key, system_key)
        lines.append(
            f"| {_escape_markdown_table_cell(system_name)} | "
            f"{hostile_counts.get(system_key, len(items))} | {len(items)} |"
        )
    lines.extend(
        (
            "",
            "| 人员 | 星系 | 军团 | 联盟 | zKill |",
            "| --- | --- | --- | --- | --- |",
        )
    )
    displayed = 0
    for system_key in active_systems:
        system_name = system_names.get(system_key, system_key)
        items = hostiles_by_system.get(system_key, [])
        for item in items:
            if displayed >= MAX_HOSTILES:
                break
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            character_id = str(item.get("character_id") or metadata.get("character_id") or "").strip()
            zkill = f"[查看](https://zkillboard.com/character/{character_id}/)" if character_id.isdigit() else "—"
            lines.append(
                "| " + " | ".join(
                    (
                        _escape_markdown_table_cell(_hostile_name(item)),
                        _escape_markdown_table_cell(system_name),
                        _escape_markdown_table_cell(_affiliation(metadata, "corporation")),
                        _escape_markdown_table_cell(_affiliation(metadata, "alliance")),
                        zkill,
                    )
                ) + " |"
            )
            displayed += 1
    return "\n".join(lines)


def _hostile_counts_by_system(
    bootstrap: dict[str, Any],
) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = {}
    display_names: dict[str, str] = {}

    def record(item: object) -> None:
        if not isinstance(item, dict):
            return
        system_name = str(
            item.get("system_name") or item.get("system") or ""
        ).strip()
        if not system_name:
            return
        try:
            count = max(0, int(item.get("hostile_count") or 0))
        except (TypeError, ValueError):
            count = 0
        system_key = system_name.casefold()
        display_names.setdefault(system_key, system_name)
        counts[system_key] = max(counts.get(system_key, 0), count)

    map_payload = bootstrap.get("map")
    map_systems = map_payload.get("systems") if isinstance(map_payload, dict) else []
    for item in map_systems if isinstance(map_systems, list) else []:
        record(item)
    raw_nodes = bootstrap.get("monitoring_nodes")
    for item in raw_nodes if isinstance(raw_nodes, list) else []:
        record(item)
    raw_alerts = bootstrap.get("alerts")
    for item in raw_alerts if isinstance(raw_alerts, list) else []:
        record(item)

    return counts, display_names


def format_monitoring_nodes(bootstrap: dict[str, Any]) -> str:
    raw_nodes = bootstrap.get("monitoring_nodes")
    nodes = [item for item in raw_nodes if isinstance(item, dict)] if isinstance(raw_nodes, list) else []
    ordered = sorted(
        nodes,
        key=lambda item: (
            str(item.get("system_name") or "").casefold(),
            str(item.get("client_id") or "").casefold(),
        ),
    )
    lines = [
        f"### 🛰️ 预警节点｜{len(ordered)}",
        "| 节点 | 状态 | 星系 | 当前敌对 |",
        "| --- | --- | --- | ---: |",
    ]
    if not ordered:
        lines.append("| 暂无节点 | — | — | 0 |")
        return "\n".join(lines)
    for index, node in enumerate(ordered, start=1):
        health = str(node.get("health_status") or "online").strip().casefold()
        status = {
            "online": "🟢 正常",
            "degraded": "🟡 连接异常",
            "offline": "⚪ 节点离线",
        }.get(health, "⚪ 节点离线")
        hostile = "—" if health == "offline" else str(max(0, int(node.get("hostile_count") or 0)))
        lines.append(
            f"| 监控节点 {index} | {status} | "
            f"{_escape_markdown_table_cell(node.get('system_name') or '未知星系')} | {hostile} |"
        )
    return "\n".join(lines)


def _online_nodes(bootstrap: dict[str, Any]) -> list[AlertNode]:
    clients = bootstrap.get("clients")
    heartbeats = clients.get("heartbeats") if isinstance(clients, dict) else None
    if not isinstance(heartbeats, list):
        return []

    nodes: dict[tuple[str, str], AlertNode] = {}
    for heartbeat in heartbeats:
        if not isinstance(heartbeat, dict):
            continue
        if heartbeat.get("client_type") != "detector_client" or not heartbeat.get("online"):
            continue
        details = heartbeat.get("details")
        if not isinstance(details, dict) or not details.get("monitoring"):
            continue
        targets = details.get("targets")
        if isinstance(targets, list) and targets:
            for target in targets:
                if not isinstance(target, dict) or not target.get("monitoring", True):
                    continue
                node = _node_from_target(target, details)
                nodes.setdefault(node.key, node)
        else:
            node = _node_from_target({}, details)
            nodes.setdefault(node.key, node)
    return list(nodes.values())


def _node_from_target(target: dict[str, Any], details: dict[str, Any]) -> AlertNode:
    system_name = str(
        target.get("system_name")
        or target.get("system")
        or details.get("system_name")
        or details.get("system")
        or "未知星系"
    ).strip()
    source_instance = str(
        target.get("source_instance")
        or target.get("window_title")
        or details.get("window")
        or ""
    ).strip()
    label = str(target.get("character_name") or "").strip()
    if not label:
        label = re.sub(r"^EVE\s*-\s*", "", source_instance, flags=re.IGNORECASE).strip()
    return AlertNode(system_name or "未知星系", source_instance, label or "监控节点")


def _current_hostiles(bootstrap: dict[str, Any]) -> list[dict[str, Any]]:
    raw_active = bootstrap.get("active_intel")
    raw_alerts = bootstrap.get("alerts")
    if not isinstance(raw_active, list) or not isinstance(raw_alerts, list):
        return []

    alerts: dict[str, dict[str, Any]] = {}
    for alert in raw_alerts:
        if not isinstance(alert, dict):
            continue
        active_id = str(alert.get("active_intel_id") or "").strip()
        if active_id:
            alerts.setdefault(active_id, alert)

    hostiles: dict[tuple[str, str, str], dict[str, Any]] = {}
    for active in raw_active:
        if not isinstance(active, dict) or active.get("active") is False:
            continue
        active_id = str(active.get("id") or "").strip()
        alert = alerts.get(active_id)
        source = str(active.get("source") or "").strip().casefold()
        if not active_id or alert is None or source not in DETECTOR_SOURCES:
            continue
        item = dict(active)
        for key in ("level", "score", "classification", "names", "metadata"):
            if item.get(key) in (None, "", [], {}) and alert.get(key) not in (
                None,
                "",
                [],
                {},
            ):
                item[key] = alert[key]
        name = _hostile_name(item)
        if not name:
            continue
        key = (
            _system_name(item).casefold(),
            str(item.get("source_instance") or "").strip().casefold(),
            name.casefold(),
        )
        hostiles.setdefault(key, item)
    return list(hostiles.values())


def _assign_hostiles(
    nodes: list[AlertNode], hostiles: list[dict[str, Any]]
) -> dict[AlertNode, list[dict[str, Any]]]:
    assignments = {node: [] for node in nodes}
    by_key = {node.key: node for node in nodes}
    by_system: dict[str, list[AlertNode]] = {}
    for node in nodes:
        by_system.setdefault(node.system_name.casefold(), []).append(node)

    for item in hostiles:
        system_name = _system_name(item)
        source_instance = str(item.get("source_instance") or "").strip()
        node = by_key.get((system_name.casefold(), source_instance.casefold()))
        if node is None:
            candidates = by_system.get(system_name.casefold(), [])
            if len(candidates) == 1:
                node = candidates[0]
        if node is None:
            label = re.sub(
                r"^EVE\s*-\s*", "", source_instance, flags=re.IGNORECASE
            ).strip()
            node = AlertNode(system_name, source_instance, label or "未关联节点")
            assignments.setdefault(node, [])
        assignments[node].append(item)
    for items in assignments.values():
        items.sort(key=lambda item: _hostile_name(item).casefold())
    return assignments


def _hostile_lines(item: dict[str, Any]) -> list[str]:
    name = _hostile_name(item) or "未知目标"
    details = [name]
    threat = _threat_label(item)
    if threat:
        details.append(threat)
    seen_at = _format_time(item.get("first_seen_at"))
    if seen_at:
        details.append(f"发现 {seen_at}")
    lines = [f"  {('｜'.join(details))}"]

    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    corporation = _affiliation(metadata, "corporation")
    alliance = _affiliation(metadata, "alliance")
    if corporation:
        lines.append(f"  军团｜{corporation}")
    if alliance:
        lines.append(f"  联盟｜{alliance}")
    return lines


def _hostile_name(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    if name:
        return name
    names = item.get("names")
    if isinstance(names, list):
        return "、".join(str(value).strip() for value in names if str(value).strip())
    return ""


def _system_name(item: dict[str, Any]) -> str:
    return str(item.get("system_name") or "未知星系").strip() or "未知星系"


def _affiliation(metadata: dict[str, Any], kind: str) -> str:
    ticker = str(metadata.get(f"{kind}_ticker") or "").strip()
    name = str(metadata.get(f"{kind}_name") or "").strip()
    if ticker and name:
        return f"[{ticker}] {name}"
    return name or (f"[{ticker}]" if ticker else "")


def _threat_label(item: dict[str, Any]) -> str:
    levels = {"low": "低", "medium": "中", "high": "高", "critical": "严重"}
    level = levels.get(str(item.get("level") or "").strip().casefold(), "")
    score = item.get("score")
    if level and isinstance(score, int | float):
        return f"{level} {score:g}"
    return level


def _format_time(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")


def _bootstrap_url(events_url: str) -> str:
    value = str(events_url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    path = re.sub(r"/events/?$", "/bootstrap", parsed.path)
    if path == parsed.path:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _ocr_query_url(bootstrap_url: str) -> str:
    parsed = urlsplit(str(bootstrap_url or "").strip())
    path = re.sub(r"/bootstrap/?$", "/ocr/query", parsed.path)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _sentry_headers(api_key: str, accept: str = "application/json") -> dict[str, str]:
    headers = {"Accept": accept}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
