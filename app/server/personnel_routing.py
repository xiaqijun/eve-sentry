"""Pin personnel dependencies per read and fence late OCR results across switches."""

from functools import wraps


def personnel_read(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if not hasattr(self, "_personnel_local"):
            return method(self, *args, **kwargs)
        if getattr(self._personnel_local, "route", None) is not None:
            return method(self, *args, **kwargs)
        with self._lock:
            self._personnel_local.route = (self._live_resolver, self._live_enricher,
                                           self._live_personnel_runtime, self._personnel_generation)
        try:
            return method(self, *args, **kwargs)
        finally:
            del self._personnel_local.route
    return wrapped


def personnel_task(method):
    pinned = personnel_read(method)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        # EsiWorker deduplicates the running key. Retry it here, not via submit(),
        # which would reject that key until this handler returns.
        while True:
            with self._lock:
                generation = self._personnel_generation
            result = pinned(self, *args, **kwargs)
            with self._lock:
                if generation == self._personnel_generation:
                    return result
    return wrapped


class PersonnelRoutingMixin:
    """Writers publish under the store lock; slow reads keep their original route."""

    @property
    def _resolver(self):
        route = getattr(getattr(self, "_personnel_local", None), "route", None)
        return route[0] if route is not None else self._live_resolver

    @_resolver.setter
    def _resolver(self, value):
        self._live_resolver = value

    @property
    def _enricher(self):
        route = getattr(getattr(self, "_personnel_local", None), "route", None)
        return route[1] if route is not None else self._live_enricher

    @_enricher.setter
    def _enricher(self, value):
        self._live_enricher = value

    @property
    def _personnel_runtime(self):
        route = getattr(getattr(self, "_personnel_local", None), "route", None)
        return route[2] if route is not None else getattr(self, "_live_personnel_runtime", None)

    @_personnel_runtime.setter
    def _personnel_runtime(self, value):
        self._live_personnel_runtime = value

    def _personnel_read_current(self):
        route = getattr(getattr(self, "_personnel_local", None), "route", None)
        return route is None or route[3] == self._personnel_generation
