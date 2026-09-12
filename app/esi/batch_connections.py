"""Reuse one thread-local transaction for bounded, SQL-only archive batches."""

import threading
from contextlib import contextmanager


class BatchConnections:
    def __init__(self, factory):
        self.factory = factory
        self.local = threading.local()

    @contextmanager
    def __call__(self):
        current = getattr(self.local, "connection", None)
        if current is not None:
            yield current
        else:
            with self.factory() as connection:
                yield connection

    @contextmanager
    def batch(self):
        if getattr(self.local, "connection", None) is not None:
            yield
            return
        with self.factory() as connection:
            self.local.connection = connection
            try:
                yield
            finally:
                del self.local.connection
