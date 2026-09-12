"""Single-process coordination for the server's shared SSO token path."""

import os
import threading
from dataclasses import dataclass, field


@dataclass
class TokenCoordinator:
    state_lock: object = field(default_factory=threading.RLock)
    refresh_lock: object = field(default_factory=threading.Lock)
    revision: int = 0


_registry_lock = threading.Lock()
_coordinators: dict[str, TokenCoordinator] = {}


def token_coordinator(path):
    key = os.path.normcase(str(path.resolve()))
    with _registry_lock:
        return _coordinators.setdefault(key, TokenCoordinator())
