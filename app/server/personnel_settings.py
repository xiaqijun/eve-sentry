"""Admin-owned personnel settings with durable, restart-free mode changes."""

from __future__ import annotations

import json
import os
from uuid import uuid4

from app.server.auth import AuthError
from app.server.intel_store import utc_now_iso

SETTING_KEY = "personnel_settings"
DEFAULTS = {"mode": "off", "background_refresh": True, "history_backfill": True,
            "background_max": 4, "organization_mode": "off"}


def validate_settings(values):
    if not isinstance(values, dict) or set(values) not in (set(DEFAULTS), set(DEFAULTS) - {"organization_mode"}):
        raise ValueError("必须完整提供人员档案配置，不能包含未知字段")
    values = {**DEFAULTS, **values}
    if not isinstance(values["mode"], str) or values["mode"] not in {"off", "shadow", "on"}:
        raise ValueError("mode 必须为 off、shadow 或 on")
    for key in ("background_refresh", "history_backfill"):
        if type(values[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    if type(values["background_max"]) is not int or not 1 <= values["background_max"] <= 4:
        raise ValueError("后台并发上限必须是 1～4 的整数")
    if values["organization_mode"] not in ("off", "shadow", "on"):
        raise ValueError("organization_mode 必须为 off、shadow 或 on")
    return dict(values)


class PersonnelSettings:
    """Persist configuration, then publish a prepared route on this instance."""

    def __init__(self, store, args, resolver, auth_service, *, environment=None):
        self.store = store
        self.auth = auth_service
        self.repository = auth_service.repository if auth_service is not None else None
        env = os.environ if environment is None else environment
        self.defaults = validate_settings({**DEFAULTS, "mode": env.get(
            "EVE_SENTRY_PERSONNEL_CACHE", "off").strip().lower()})
        self.available = args.storage == "postgres" and resolver is not None
        from app.esi.personnel_control import personnel_controller
        self.controller = personnel_controller(store, args, resolver)
        self._lock = self.controller.lock

    def _read(self):
        try:
            raw = self.repository.setting(SETTING_KEY) if self.repository is not None else None
            if raw is None:
                return None, "environment", dict(self.defaults)
            document = json.loads(raw)
            if not isinstance(document["revision"], str) or not document["revision"]:
                raise ValueError("invalid saved revision")
            return raw, document["revision"], validate_settings(document["values"])
        except Exception as exc:
            raise AuthError("人员档案配置读取失败，请检查配置存储后重试", 503, "personnel_settings_storage_error") from exc

    def startup_values(self):
        return self._read()[2]

    def _snapshot(self, revision, values):
        runtime = getattr(self.store, "_personnel_runtime", None)
        effective = {"mode": runtime.mode if runtime is not None else "off",
                     "organization_mode": getattr(runtime, "organization_mode", "off"),
                     **(runtime.scheduling_settings() if runtime is not None else {
                         "background_refresh": False, "history_backfill": False, "background_max": 0})}
        comparisons = getattr(getattr(self.controller.resources, "enricher", None), "shadow_comparisons", None)
        return {"values": values, "effective": effective, "revision": revision,
                "organization_shadow": comparisons.snapshot() if comparisons is not None else {},
                "source": "environment" if revision == "environment" else "database",
                "restart_required": False,
                "apply_required": values["mode"] != effective["mode"] or (runtime is not None and values != effective),
                "available": self.available,
                "unavailable_reason": "" if self.available else "需要 PostgreSQL 存储并启用 ESI",
                "writable": self.repository is not None}

    def snapshot(self):
        with self._lock:
            _, revision, values = self._read()
            return self._snapshot(revision, values)

    def update(self, payload, actor_user_id):
        try:
            return self._update(payload, actor_user_id)
        except (AuthError, ValueError):
            raise
        except Exception as exc:
            raise AuthError("配置保存结果未确认，请重新读取后核对", 503, "personnel_settings_storage_error") from exc

    def _update(self, payload, actor_user_id):
        if not isinstance(payload, dict) or set(payload) != {"values", "revision"}:
            raise ValueError("必须提供 values 和 revision")
        values = validate_settings(payload["values"])
        if not isinstance(payload["revision"], str):
            raise ValueError("revision 必须为字符串")
        if self.repository is None:
            raise AuthError("配置持久化不可用", 409, "personnel_settings_unavailable")
        if values["mode"] != "off" and not self.available:
            raise AuthError("需要 PostgreSQL 存储并启用 ESI", 409, "personnel_settings_unavailable")
        with self._lock:
            raw, revision, previous = self._read()
            if payload["revision"] != revision:
                raise AuthError("配置已被其他管理员修改，请重新读取后保存", 409, "settings_conflict")
            if "organization_mode" not in payload["values"]:
                values["organization_mode"] = previous["organization_mode"]
            if previous == values:
                prepared = self.controller.prepare(values)
                self.controller.publish(values, prepared)
                return self._snapshot(revision, values)
            next_revision = uuid4().hex
            encoded = json.dumps({"revision": next_revision, "values": values}, sort_keys=True)
            now = utc_now_iso()
            audit = self.auth._audit_record(actor_user_id, actor_user_id,
                                           "personnel.settings_changed",
                                           {"previous": previous, "values": values}, now)
            # Setting and audit commit together. Compare raw persisted data to
            # reject races between independent HTTP handlers/service instances.
            prepared = self.controller.prepare(values)
            try:
                with self.repository._connect() as connection:
                    if raw is None:
                        result = connection.execute(
                            "INSERT INTO auth_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(setting_key) DO NOTHING", (SETTING_KEY, encoded, now))
                    else:
                        result = connection.execute(
                            "UPDATE auth_settings SET setting_value = ?, updated_at = ? "
                            "WHERE setting_key = ? AND setting_value = ?", (encoded, now, SETTING_KEY, raw))
                    if result.rowcount != 1:
                        raise AuthError("配置已发生变化，请重新读取后保存", 409, "settings_conflict")
                    self.repository._insert_audit(connection, audit)
            except Exception:
                self.controller.discard(prepared)
                raise
            self.controller.publish(values, prepared)
            return self._snapshot(next_revision, values)
