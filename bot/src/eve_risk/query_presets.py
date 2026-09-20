"""Group-scoped query shortcuts; store conditions, never query results."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import WatchError

from eve_risk.parser import normalize_command_content

MAX_PRESETS = 7
TARGET_FIELDS = {
    "人员": ("filtered", "name"),
    "军团": ("filtered", "corporation"),
    "联盟": ("filtered", "alliance"),
    "星系": ("system_roster", "system_name"),
}
PRESET_HELP = (
    "添加：预设 监控 Alice（省略类型按人员查询）。\n"
    "也可：预设 监控 星系 S-KSWL / 预设 监控 军团 Blue Corp / 预设 监控 联盟 Example。\n"
    "查看：预设列表；删除自己的预设：删除预设 编号。\n"
    "本群成员共享按钮，点击查询一次，不自动周期监测。"
)


class PresetError(ValueError):
    """Safe, user-facing preset validation error."""


@dataclass(frozen=True)
class PresetCommand:
    action: str
    kind: str = ""
    target: str = ""
    preset_id: str = ""
    error: str = ""


def parse_preset_command(content: str) -> PresetCommand | None:
    text = normalize_command_content(content)
    if text in {"预设", "预设列表"}:
        return PresetCommand("list")
    match = re.fullmatch(r"删除预设(?:\s+(.+))?", text)
    if match:
        preset_id = (match.group(1) or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", preset_id):
            return PresetCommand("error", error="请发送：删除预设 编号（编号可通过预设列表查看）。")
        return PresetCommand("delete", preset_id=preset_id)
    match = re.fullmatch(r"预设[\s，,:：]+监控(?:[\s:：]+(.*))?", text, re.DOTALL)
    if not match:
        if re.match(r"^预设(?:[\s，,:：]|$)", text):
            return PresetCommand("error", error=PRESET_HELP)
        return None
    target = (match.group(1) or "").strip()
    kind = "人员"
    typed = re.fullmatch(r"(人员|角色|军团|联盟|星系)(?:[\s:：]+(.*))?", target, re.DOTALL)
    if typed:
        kind = "人员" if typed.group(1) == "角色" else typed.group(1)
        target = (typed.group(2) or "").strip()
    if not target or len(target) > 100 or any(ord(ch) < 32 for ch in target):
        return PresetCommand("error", error="名称须为 1–100 字的单行文本。例如：预设 监控 星系 S-KSWL。")
    return PresetCommand("add", kind=kind, target=target)


@dataclass(frozen=True)
class QueryPreset:
    preset_id: str
    kind: str
    target: str
    owner: str

    @property
    def query(self) -> dict[str, str]:
        mode, field = TARGET_FIELDS[self.kind]
        return {"mode": mode, field: self.target}

    @property
    def label(self) -> str:
        name = self.target if len(self.target) <= 16 else self.target[:15] + "…"
        return f"{self.kind}·{name}"


class QueryPresetStore:
    def __init__(self, redis: Redis, group: str):
        if not group:
            raise PresetError("缺少群信息。")
        self.redis = redis
        self.key = "qq:query:presets:" + hashlib.sha256(group.encode()).hexdigest()

    @staticmethod
    def _decode(preset_id: bytes | str, raw: bytes | str) -> QueryPreset:
        try:
            pid = preset_id.decode() if isinstance(preset_id, bytes) else preset_id
            value = json.loads(raw)
            if not re.fullmatch(r"[0-9a-f]{12}", pid) or not isinstance(value, dict):
                raise ValueError
            kind, target, owner = value["kind"], value["target"], value["owner"]
            if not isinstance(kind, str) or kind not in TARGET_FIELDS:
                raise ValueError
            if not isinstance(target, str) or not target.strip() or len(target) > 100:
                raise ValueError
            if any(ord(ch) < 32 for ch in target):
                raise ValueError
            if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{64}", owner):
                raise ValueError
            return QueryPreset(pid, kind, target, owner)
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise PresetError("预设数据异常，请联系管理员检查。") from exc

    async def list(self) -> list[QueryPreset]:
        values = await self.redis.hgetall(self.key)
        return [self._decode(pid, raw) for pid, raw in sorted(values.items())]

    async def get(self, preset_id: str) -> QueryPreset | None:
        if not re.fullmatch(r"[0-9a-f]{12}", preset_id):
            return None
        raw = await self.redis.hget(self.key, preset_id)
        return self._decode(preset_id, raw) if raw is not None else None

    async def add(self, member: str, kind: str, target: str) -> tuple[QueryPreset, bool]:
        owner = hashlib.sha256(member.encode()).hexdigest()
        if not member:
            raise PresetError("缺少成员信息。")
        payload = json.dumps({"kind": kind, "target": target, "owner": owner}, ensure_ascii=False)
        for _ in range(8):
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(self.key)
                    values = await pipe.hgetall(self.key)
                    existing = [self._decode(pid, raw) for pid, raw in values.items()]
                    for item in existing:
                        if item.kind == kind and item.target.casefold() == target.casefold():
                            return item, False
                    if len(existing) >= MAX_PRESETS:
                        raise PresetError(f"本群最多保存 {MAX_PRESETS} 个预设，请先删除不再使用的预设。")
                    preset = self._decode(uuid.uuid4().hex[:12], payload)
                    if any(item.preset_id == preset.preset_id for item in existing):
                        continue
                    pipe.multi()
                    pipe.hset(self.key, preset.preset_id, payload)
                    await pipe.execute()
                    return preset, True
                except WatchError:
                    continue
        raise PresetError("预设正在被其他成员修改，请稍后重试。")

    async def delete(self, member: str, preset_id: str) -> None:
        # IDs are immutable and never updated in place, so owner cannot change
        # between this read and deletion. A second deletion is harmless.
        preset = await self.get(preset_id)
        if preset is None:
            raise PresetError("本群没有这个预设，请发送预设列表核对编号。")
        if preset.owner != hashlib.sha256(member.encode()).hexdigest():
            raise PresetError("只能删除自己创建的预设。")
        await self.redis.hdel(self.key, preset_id)
