"""Prepare, publish and retire personnel runtimes without restarting the server."""

import threading
from dataclasses import dataclass

from app.server.auth import AuthError


@dataclass
class PersonnelResources:
    pool: object
    runtime: object
    resolver: object
    enricher: object


class PersonnelController:
    def __init__(self, store, args, resolver):
        self.store, self.args = store, args
        self.original_resolver = resolver
        self.original_enricher = getattr(store, "_enricher", None)
        self.lock = threading.RLock()
        self.resources = None
        self.retired = []
        self.closed = False

    def prepare(self, values):
        """Called with controller lock, never with the store mutation lock."""
        if self.closed:
            raise AuthError("服务正在关闭，请稍后重试", 503, "personnel_settings_unavailable")
        if values["mode"] == "off":
            return None
        if self.resources is not None:
            return self.resources
        self.retired = [thread for thread in self.retired if thread.is_alive()]
        if len(self.retired) >= 2:
            raise AuthError("旧档案任务仍在收尾，请稍后重新保存", 409, "personnel_switch_busy")
        from app.esi.personnel_setup import PersonnelEnricher
        from app.esi.personnel_archive import PersonnelArchive
        from app.esi.personnel_runtime import PersonnelResolver, PersonnelRuntime
        from app.esi.personnel_backfill import PersonnelBackfill
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from psycopg.conninfo import make_conninfo, conninfo_to_dict

        options = conninfo_to_dict(self.args.postgres_dsn).get("options", "")
        dsn = make_conninfo(self.args.postgres_dsn, options=options + " -cstatement_timeout=5000 -clock_timeout=2000")
        pool = ConnectionPool(dsn, min_size=1, max_size=8, timeout=2, open=True, kwargs={"row_factory": dict_row})
        runtime = None
        try:
            pool.wait(timeout=2)
            archive = PersonnelArchive(pool.connection)
            archive.migrate()
            runtime = PersonnelRuntime(archive, self.original_resolver.client)
            runtime.backfill = PersonnelBackfill(archive, self.original_resolver.cache)
            replacement = PersonnelResolver(self.original_resolver, runtime)
            enricher = PersonnelEnricher(replacement, getattr(self.original_enricher, "esi_session", None))
            # Prove thread creation works before saving. No SQL/ESI tasks run
            # until publication activates this gate after the DB commit.
            runtime.start(lambda changed, all_current: self.store.refresh_personnel(
                changed, all_current, expected_runtime=runtime), paused=True)
            return PersonnelResources(pool, runtime, replacement, enricher)
        except Exception as exc:
            if runtime is not None:
                runtime.close(timeout=None)
            pool.close()
            raise AuthError("档案初始化失败，原模式保持不变；请检查数据库后重试", 503,
                            "personnel_switch_failed") from exc

    def discard(self, prepared):
        if prepared is not None and prepared is not self.resources:
            prepared.runtime.close(timeout=None)
            prepared.pool.close()

    def publish(self, values, prepared):
        old = self.resources
        with self.store._lock:
            runtime = self.store._personnel_runtime
            previous_mode = runtime.mode if runtime is not None else "off"
            mode = values["mode"]
            if mode != previous_mode:
                self.store._personnel_generation += 1
                self.store._resolver = prepared.resolver if mode == "on" else self.original_resolver
                self.store._enricher = prepared.enricher if mode == "on" else self.original_enricher
                self.store._personnel_runtime = prepared.runtime if prepared else None
                self.store._personnel_pool = prepared.pool if prepared else None
                self.store._alert_cache.clear()
                self.store._character_profile_cache.clear()
            self.resources = prepared
            if prepared is not None:
                prepared.runtime.mode = mode
                prepared.runtime.contact_refresher = prepared.enricher.refresh_contacts if mode == "on" else None
                prepared.runtime.configure_scheduling(**{key: value for key, value in values.items() if key != "mode"})
                prepared.runtime.activate()
        if old is not None and old is not prepared:
            old.runtime.request_stop()
            thread = threading.Thread(target=self._retire, args=(old,), name="personnel-retire", daemon=True)
            self.retired.append(thread)
            thread.start()

    @staticmethod
    def _retire(resources):
        # The executor may still be completing ESI/SQL calls. Never close its
        # pool merely because a timed join expired.
        resources.runtime.close(timeout=None)
        resources.pool.close()

    def close(self):
        with self.lock:
            self.closed = True
            if self.resources is not None:
                self._retire(self.resources)
                self.resources = None
            for thread in self.retired:
                thread.join()
            self.retired.clear()


def personnel_controller(store, args, resolver):
    with getattr(store, "_lock", threading.RLock()):
        controller = getattr(store, "_personnel_controller", None)
        if controller is None:
            controller = PersonnelController(store, args, resolver)
            store._personnel_controller = controller
        return controller
