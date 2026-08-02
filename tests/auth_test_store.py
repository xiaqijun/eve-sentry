"""Test-only JSON intel store with an in-memory authentication database."""

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from threading import RLock
from typing import Any

from app.server.auth_store import migrate_auth_schema
from app.server.intel_store import IntelStore


class AuthTestStore(IntelStore):
    """Keep intel data in JSON while providing SQL-backed auth test state."""

    def __init__(self, path: str | Path, **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        self._auth_connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._auth_connection.row_factory = sqlite3.Row
        self._auth_connection_lock = RLock()
        migrate_auth_schema(self._auth_connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._auth_connection_lock:
            with self._auth_connection as connection:
                yield connection

    def close(self, *, wait: bool = True) -> None:
        super().close(wait=wait)
        self._auth_connection.close()
