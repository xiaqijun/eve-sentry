"""Expiry and source failover run independently of HTTP readers."""

import logging
import threading

logger = logging.getLogger(__name__)


class StateMaintenance:
    def __init__(self, store, *, interval: float = 1.0):
        self.store = store
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="system-state-maintenance", daemon=True
        )
        self.thread.start()

    def run(self):
        while not self.stopped.wait(self.interval):
            try:
                repair = getattr(self.store, "_state_repair", None)
                changed = repair.run() if repair is not None else 0
                changed += self.store.expire_active_intel()
                notifier = getattr(self.store, "_change_notifier", None)
                if changed and callable(notifier):
                    notifier()
            except Exception:
                logger.exception(
                    "System state maintenance failed; retrying next interval"
                )

    def close(self, *, wait: bool = True):
        self.stopped.set()
        if wait:
            self.thread.join(timeout=5.0)
