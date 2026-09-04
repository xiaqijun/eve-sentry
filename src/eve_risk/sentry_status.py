from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

QUERY_COMMANDS = {"查询预警", "预警详情", "敌对详情", "节点敌对"}
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
        filters: dict[str, str] | None = None,
        *,
        refresh: bool = False,
    ) -> str:
        if not self.enabled:
            raise SentryStatusError("预警服务尚未配置，请联系机器人管理员。")
        if refresh:
            return await self._query_ocr(filters or {})
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
        return format_sentry_status(bootstrap)

    async def _query_ocr(self, filters: dict[str, str]) -> str:
        query_url = _ocr_query_url(self.bootstrap_url)
        try:
            response = await self.http.post(
                query_url,
                headers=_sentry_headers(self.api_key),
                json={key: value for key, value in filters.items() if value},
                timeout=10.0,
            )
            response.raise_for_status()
            created = response.json()
            query_id = str(created.get("query_id") or "").strip()
            if not query_id:
                raise SentryStatusError("预警服务未返回 OCR 查询编号。")
            status_url = f"{query_url}/{query_id}"
            deadline = asyncio.get_running_loop().time() + 35.0
            while asyncio.get_running_loop().time() < deadline:
                status_response = await self.http.get(
                    status_url,
                    headers=_sentry_headers(self.api_key),
                    timeout=10.0,
                )
                status_response.raise_for_status()
                status = status_response.json()
                if str(status.get("status") or "") in {"completed", "timed_out"}:
                    return format_ocr_query(status, filters)
                await asyncio.sleep(0.5)
        except SentryStatusError:
            raise
        except Exception:
            logger.exception("EVE Sentry OCR query failed")
            raise SentryStatusError("OCR 查询失败或客户端未响应，请稍后重试。") from None
        raise SentryStatusError("OCR 查询超时，当前没有收到客户端回传。")


def is_sentry_status_command(content: str) -> bool:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(
        r"^\s*(?:<@!?\w+>|@\S+)\s*", "", normalized, count=1
    ).strip()
    normalized = re.sub(r"^/", "", normalized, count=1).strip()
    return normalized in QUERY_COMMANDS


def parse_sentry_query(content: str) -> dict[str, str] | None:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"^\s*(?:<@!?\w+>|@\S+)\s*", "", normalized, count=1)
    normalized = re.sub(r"^/", "", normalized, count=1).strip()
    match = re.match(
        r"^(?:查询预警|预警详情|敌对详情|节点敌对)(?:\s+(.+))?$",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    argument = str(match.group(1) or "").strip()
    if not argument:
        return {}
    key = "name"
    for prefix, candidate_key in (("人员", "name"), ("角色", "name"), ("军团", "corporation"), ("联盟", "alliance")):
        if argument.startswith(prefix):
            argument = argument[len(prefix):].strip()
            key = candidate_key
            break
    return {key: argument} if argument else {}


def format_ocr_query(payload: dict[str, Any], filters: dict[str, str] | None = None) -> str:
    results = payload.get("results")
    results = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
    lines = [
        f"OCR 查询｜节点 {len(results)}/{int(payload.get('expected_clients') or len(results))}"
    ]
    displayed = 0
    for result in results:
        system = str(result.get("system_name") or "未知星系").strip()
        recognized = result.get("recognized")
        recognized = [item for item in recognized if isinstance(item, dict)] if isinstance(recognized, list) else []
        selected = [item for item in recognized if _query_item_matches(item, filters or {})]
        raw_names = [str(name).strip() for name in result.get("names", []) if str(name).strip()]
        if not selected and filters and raw_names:
            continue
        lines.append(f"\n{system}｜识别 {len(selected) if filters else len(recognized)} 人")
        if selected:
            for item in selected[:MAX_HOSTILES]:
                lines.extend(_hostile_lines(item))
                displayed += 1
        elif raw_names:
            lines.append(f"  OCR 原始名单｜{'、'.join(raw_names[:MAX_HOSTILES])}")
    if len(lines) == 1:
        return "OCR 查询｜没有收到符合条件的人员名单。"
    return "\n".join(lines)


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
    nodes = _online_nodes(bootstrap)
    hostiles = _current_hostiles(bootstrap)
    assignments = _assign_hostiles(nodes, hostiles)

    if not nodes and not hostiles:
        return "预警节点｜当前无在线监控节点"

    total_hostiles = sum(len(items) for items in assignments.values())
    lines = [f"预警节点｜在线 {len(nodes)}｜敌对 {total_hostiles} 人"]
    displayed_hostiles = 0
    displayed_nodes = 0

    ordered_nodes = sorted(
        assignments,
        key=lambda node: (
            not bool(assignments[node]),
            node.system_name.casefold(),
            node.label.casefold(),
        ),
    )
    for node_index, node in enumerate(ordered_nodes, start=1):
        if displayed_nodes >= MAX_NODES:
            break
        items = assignments[node]
        icon = "🔴" if items else "🟢"
        lines.append(
            f"{icon} {node.system_name}｜敌 {len(items)}｜监控节点 {node_index}"
        )
        displayed_nodes += 1
        for item in items:
            if displayed_hostiles >= MAX_HOSTILES:
                break
            lines.extend(_hostile_lines(item))
            displayed_hostiles += 1

    omitted_nodes = max(0, len(ordered_nodes) - displayed_nodes)
    omitted_hostiles = max(0, total_hostiles - displayed_hostiles)
    if omitted_nodes or omitted_hostiles:
        parts = []
        if omitted_nodes:
            parts.append(f"{omitted_nodes} 个节点")
        if omitted_hostiles:
            parts.append(f"{omitted_hostiles} 名敌对")
        lines.append(f"其余｜{'、'.join(parts)}未展开")
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
