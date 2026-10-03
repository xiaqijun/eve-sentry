"""Expiry and source failover run independently of HTTP readers."""

import logging
import threading

logger = logging.getLogger(__name__)


def _monitoring_heartbeat_fingerprint(snapshot):
    """Return the stable node fields used to detect heartbeat transitions."""
    if not isinstance(snapshot, dict):
        return ()
    heartbeats = snapshot.get("heartbeats")
    if not isinstance(heartbeats, list):
        return ()
    states = []
    for heartbeat in heartbeats:
        if not isinstance(heartbeat, dict):
            continue
        if str(heartbeat.get("client_type") or "").strip() != "detector_client":
            continue
        details = heartbeat.get("details")
        details = details if isinstance(details, dict) else {}
        targets = details.get("targets")
        if not isinstance(targets, list) or not targets:
            targets = [details]
        target_states = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            capture_online = target.get("capture_online")
            capture_state = "" if capture_online is None else (
                "1" if bool(capture_online) else "0"
            )
            target_states.append(
                (
                    str(target.get("client_id") or "").strip(),
                    str(
                        target.get("system_name") or target.get("system") or ""
                    ).strip().casefold(),
                    bool(target.get("monitoring", True)),
                    capture_state,
                )
            )
        states.append(
            (
                str(heartbeat.get("client_id") or "").strip(),
                str(heartbeat.get("health_status") or "").strip().casefold(),
                bool(details.get("monitoring")),
                tuple(sorted(target_states)),
            )
        )
    return tuple(sorted(states))


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
        heartbeat_fingerprint = _monitoring_heartbeat_fingerprint(
            self.store.heartbeat_snapshot()
            if callable(getattr(self.store, "heartbeat_snapshot", None))
            else None
        )
        while not self.stopped.wait(self.interval):
            try:
                repair = getattr(self.store, "_state_repair", None)
                changed = repair.run() if repair is not None else 0
                changed += self.store.expire_active_intel()
                heartbeat_snapshot = getattr(self.store, "heartbeat_snapshot", None)
                current_heartbeat_fingerprint = (
                    _monitoring_heartbeat_fingerprint(heartbeat_snapshot())
                    if callable(heartbeat_snapshot)
                    else heartbeat_fingerprint
                )
                heartbeat_changed = (
                    current_heartbeat_fingerprint != heartbeat_fingerprint
                )
                heartbeat_fingerprint = current_heartbeat_fingerprint
                notifier = getattr(self.store, "_change_notifier", None)
                if (changed or heartbeat_changed) and callable(notifier):
                    notifier()
            except Exception:
                logger.exception(
                    "System state maintenance failed; retrying next interval"
                )

    def close(self, *, wait: bool = True):
        self.stopped.set()
        if wait:
            self.thread.join(timeout=5.0)
