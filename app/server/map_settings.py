"""Validated, atomic monitoring-map configuration and SDE catalogue."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from app.server.monitoring_scope import MonitoringScope

_SETTINGS_LOCK = threading.RLock()
_CATALOGUES: dict[str, tuple[list[dict], list[dict]]] = {}


def settings_path(config) -> Path:
    # A runtime sidecar is not part of the immutable deployment archive.
    return config.path.with_suffix(".monitoring.json")


def restore_monitoring_config(config) -> bool:
    path = settings_path(config)
    if not path.exists():
        return False
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("monitoring_enabled"), bool):
        raise ValueError("invalid persisted monitoring map configuration")
    config._config = config._finalize(raw)
    return True


def install_scope(store, config) -> None:
    if config is None:
        return
    data = config.to_dict()
    with store._lock:
        store._monitoring_scope = MonitoringScope(
            enabled=bool(data.get("monitoring_enabled")),
            version=str(data.get("monitoring_version") or ""),
            systems=tuple(sorted((s.name, s.system_id) for s in store._systems.values())),
        )


def catalogue(config) -> tuple[list[dict], list[dict]]:
    """Load static SDE only on an explicit administrator settings request."""
    from app.server.sde_map import SdeMapImporter

    path = config.sde_path
    if not path:
        raise ValueError("请先配置服务端 SDE 数据目录，再配置监控区域")
    with _SETTINGS_LOCK:
        if path not in _CATALOGUES:
            systems, links = SdeMapImporter(path).load_map()
            _CATALOGUES[path] = (systems, links)
        return _CATALOGUES[path]


def settings_payload(config) -> dict:
    systems, _ = catalogue(config)
    regions = {s["region_id"]: s["region"] for s in systems if s.get("region_id")}
    current = config.to_dict()
    return {
        "enabled": bool(current.get("monitoring_enabled")),
        "version": str(current.get("monitoring_version") or ""),
        "region_ids": current["region_ids"], "system_ids": current["system_ids"],
        "excluded_system_ids": current.get("excluded_system_ids", []),
        "regions": [{"id": key, "name": value} for key, value in sorted(regions.items())],
        "systems": [{"id": s["system_id"], "name": s["name"], "region_id": s.get("region_id")}
                    for s in systems],
    }


def _ids(payload: dict, key: str, allowed: set[int]) -> list[int]:
    values = payload.get(key)
    if not isinstance(values, list) or len(values) > 15000:
        raise ValueError(f"{key} must be a bounded array of IDs")
    if any(type(value) is not int or value not in allowed for value in values):
        raise ValueError(f"{key} contains an unknown ID")
    return sorted(set(values))


def _project_selection(systems: list[dict]) -> list[dict]:
    """Project the selection before rounding, not the whole universe thumbnail."""
    if not systems or any(s.get("sde_x") is None or s.get("sde_y") is None for s in systems):
        return systems
    min_x, max_x = min(s["sde_x"] for s in systems), max(s["sde_x"] for s in systems)
    min_y, max_y = min(s["sde_y"] for s in systems), max(s["sde_y"] for s in systems)
    return [{**s, "x": round(80 + (s["sde_x"] - min_x) / max(max_x - min_x, 1.0) * 1200, 1),
             "y": round(80 + (max_y - s["sde_y"]) / max(max_y - min_y, 1.0) * 820, 1)} for s in systems]


def update_settings(config, store, payload: dict) -> dict:
    """Validate everything before replacing the durable and in-memory configuration."""
    from app.server.intel_store import StarSystem

    if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
        raise ValueError("enabled must be a boolean")
    with _SETTINGS_LOCK:
        current = config.to_dict()
        if payload.get("version", "") != current.get("monitoring_version", ""):
            raise ValueError("配置已被其他管理员修改，请重新加载后保存")
        all_systems, all_links = catalogue(config)
        region_ids = _ids(payload, "region_ids", {s["region_id"] for s in all_systems if s.get("region_id")})
        known_ids = {s["system_id"] for s in all_systems}
        system_ids = _ids(payload, "system_ids", known_ids)
        excluded = _ids(payload, "excluded_system_ids", known_ids)
        if not region_ids and not system_ids:
            raise ValueError("请至少选择一个星域或星系")
        selected = [s for s in all_systems if s["system_id"] not in excluded
                    and (s.get("region_id") in region_ids or s["system_id"] in system_ids)]
        if not selected:
            raise ValueError("排除后没有可监控星系，请调整范围")
        selected = _project_selection(selected)
        names = {s["name"] for s in selected}
        links = [link for link in all_links if link["from"] in names and link["to"] in names]
        candidate = config._finalize({**current, "source": "sde", "layout_mode": "sde",
            "region_ids": region_ids, "system_ids": system_ids, "excluded_system_ids": excluded,
            "systems": selected, "links": links, "monitoring_enabled": payload["enabled"],
            "monitoring_version": uuid4().hex})
        systems = {s["name"]: StarSystem(s["name"], s["x"], s["y"], s["region"], s.get("security"), s["system_id"])
                   for s in selected}
        # Finish retiring the previous scope before expanding it again. This
        # prevents rapid remove/re-add from resurrecting old current intel.
        store.expire_active_intel()
        path = settings_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(candidate, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            # No ESI/network work or database wait under the real-time store lock.
            with store._lock:
                os.replace(temp_path, path)
                config._config = candidate
                store.set_map_data(systems, [(l["from"], l["to"]) for l in links], allow_unmapped_systems=False)
                install_scope(store, config)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        return {"enabled": candidate["monitoring_enabled"], "version": candidate["monitoring_version"],
                "region_ids": region_ids, "system_ids": system_ids, "excluded_system_ids": excluded,
                "system_count": len(systems)}


def handle_settings(handler, *, save: bool = False) -> None:
    config = handler._map_config_store()
    if config is None:
        handler._send_json({"error": "map configuration is unavailable"}, 503)
        return
    try:
        result = update_settings(config, handler._store(), handler._read_json()) if save else settings_payload(config)
    except ValueError as exc:
        handler._send_json({"error": str(exc)}, 400)
        return
    except Exception:
        handler._send_json({"error": "星图配置加载或保存失败，请检查 SDE 和配置目录权限"}, 503)
        return
    if save:
        from app.server.http_server import _notify_event_streams
        _notify_event_streams()
    handler._send_json({"settings": result})
