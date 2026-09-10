"""Published snapshots shared by realtime event consumers."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActiveEventSnapshot:
    """One cache generation; owned payloads must not change after publication."""

    generation: int
    created_at: float
    state_event_seq: int
    state: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]
    report_cursors: dict[str, tuple[int, str]] | None = None

    def is_fresh(self, generation: int, now: float, ttl: float, minimum_seq: int) -> bool:
        return (
            self.generation == generation
            and now - self.created_at < ttl
            and self.state_event_seq >= minimum_seq
        )

    def copy_result(
        self,
        ready: bool,
        report_cursors: dict[str, tuple[int, str]] | None = None,
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int, bool,
    ]:
        """Copy mutable consumer data without holding the shared builder lock."""
        if report_cursors is not None:
            report_cursors.clear()
            report_cursors.update(self.report_cursors or {})
        return (*copy.deepcopy(self.state), self.state_event_seq, ready)
