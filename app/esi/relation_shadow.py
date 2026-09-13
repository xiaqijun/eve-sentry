"""Shadow comparisons return legacy results and never publish a second alert."""

import threading
from collections import Counter


class ShadowComparisons:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = Counter()

    def compare(self, legacy, proposed):
        def sign(profile):
            value = profile.get("contact_standing")
            return "pending" if value is None else "friendly" if value > 0 else "hostile"

        before, after = sign(legacy), sign(proposed)
        with self._lock:
            self._counts["compared"] += 1
            self._counts["different" if before != after else "same"] += 1
            self._counts[f"{before}_to_{after}"] += 1
            pending = "pending" in (before, after)
            # Keep legacy totals; only two known decisions form a comparison.
            self._counts["pending"] += int(pending)
            self._counts["comparable"] += int(not pending)
            self._counts["decision_different"] += int(not pending and before != after)

    def snapshot(self):
        with self._lock:
            return {"comparable": 0, "pending": 0, "decision_different": 0, **self._counts}


class ShadowContacts(list):
    def __init__(self, contacts, proposed, comparisons):
        super().__init__(contacts)
        self.proposed, self.comparisons = proposed, comparisons

    def compare(self, profile, legacy):
        self.comparisons.compare(legacy, self.proposed.annotate(profile))
