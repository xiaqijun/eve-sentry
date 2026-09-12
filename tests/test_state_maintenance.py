"""Maintenance does not need traffic and wakes readers only for real changes."""

import threading
from types import SimpleNamespace

from app.server.state_maintenance import StateMaintenance


def test_maintenance_runs_without_http_traffic_and_stops():
    wake = threading.Event()
    store = SimpleNamespace(expire_active_intel=lambda: 1, _change_notifier=wake.set)
    maintenance = StateMaintenance(store, interval=0.01)
    try:
        assert wake.wait(1)
    finally:
        maintenance.close()
    assert not maintenance.thread.is_alive()


def test_idle_maintenance_does_not_invalidate_snapshot_cache():
    tick = threading.Event()
    wakes = []

    def expire():
        tick.set()
        return 0

    maintenance = StateMaintenance(
        SimpleNamespace(
            expire_active_intel=expire, _change_notifier=lambda: wakes.append(1)
        ),
        interval=0.01,
    )
    try:
        assert tick.wait(1)
    finally:
        maintenance.close()
    assert wakes == []
