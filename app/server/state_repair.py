"""Retry failed in-memory state mutations as one ordered PostgreSQL snapshot."""

import copy
import json
import threading

from app.core.models import utc_now_iso
from app.server.capture_state import reconcile_zero_events
from app.server.source_authority import primary_sources


def mark_failed_write(store):
    repair = getattr(store, "_state_repair", None)
    if repair is not None:
        repair.mark()


class StateRepair:
    """Failure-only repair; ordinary capture frames do not scan the database."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.runner = threading.Lock()
        self.generation = 0
        self.completed = 0

    def mark(self):
        with self.lock:
            self.generation += 1

    def run(self):
        if not self.runner.acquire(blocking=False):
            return 0
        try:
            with self.lock:
                generation = self.generation
                if generation == self.completed:
                    return 0
            store = self.store
            # Reserve at the same mutation point as normal writes. Later
            # updates cannot be overwritten by this recovery snapshot.
            with store._lock:
                items = copy.deepcopy(list(store._active_intel.values()))
                reports = copy.deepcopy(list(store._reports))
                after = store._hostile_system_state(items)
                ticket = store._reserve_db_write()
            now = utc_now_iso()
            store._wait_for_db_write(ticket)
            try:
                with store._connect() as connection:
                    for report in reports:
                        store._upsert_report_with_connection(connection, report)
                    store._upsert_active_intel_rows(
                        connection, [store._active_row(item) for item in items]
                    )
                    rows = connection.execute(
                        "SELECT system_key, payload_json FROM system_current_state"
                    ).fetchall()
                    before = {}
                    for row in rows:
                        payload = json.loads(row["payload_json"])
                        if int(payload.get("hostile_count") or 0) > 0:
                            before[row["system_key"]] = {
                                **payload,
                                "system_key": row["system_key"],
                                "personnel": payload.get("hostile_personnel") or [],
                            }
                    primaries = primary_sources(items)
                    lost = {
                        item.system_name.casefold(): "node_offline"
                        for item in items
                        if item.metadata.get("presence_only")
                        and item.metadata.get("left_reason")
                        and item.system_name.casefold() not in primaries
                    }
                    events = store._hostile_state_events(
                        before, after, now, clear_reasons=lost
                    )
                    events = reconcile_zero_events(
                        events,
                        items,
                        now,
                        previous_zeros=[
                            item
                            for item in items
                            if item.metadata.get("presence_only")
                            and item.metadata.get("hostile_icon_count") == 0
                        ],
                        positive_systems=after,
                    )
                    store._persist_hostile_wave_changes(
                        connection,
                        store._hostile_wave_changes(before, now, after=after),
                    )
                    store._persist_intel_events(connection, events)
            finally:
                store._finish_db_write(ticket)
            with self.lock:
                self.completed = generation
            store._resume_pending_ocr_esi_tasks()
            return 1
        finally:
            self.runner.release()
