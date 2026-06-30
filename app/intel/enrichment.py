"""Optional ESI and killboard enrichment for threat scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import Observation
from app.killboard.analyzer import (
    GroupKillActivity,
    KillActivity,
    SystemKillActivity,
    analyze_character_activity,
    analyze_group_activity,
    analyze_system_activity,
)


@dataclass(frozen=True)
class ThreatEnrichment:
    """Supplemental data collected for one observation."""

    character_profiles: list[dict[str, Any]] = field(default_factory=list)
    kill_activities: list[KillActivity] = field(default_factory=list)

    def has_data(self) -> bool:
        """Return whether any enrichment source produced usable data."""
        return bool(self.character_profiles or self.kill_activities)


class ThreatEnricher:
    """Collect public ESI profiles and killboard activity for scoring."""

    def __init__(
        self,
        resolver: Any | None = None,
        killboard: Any | None = None,
        kill_window: str = "recent",
    ) -> None:
        self.resolver = resolver
        self.killboard = killboard
        self.kill_window = kill_window

    def enrich(self, observation: Observation) -> ThreatEnrichment:
        """Return best-effort enrichment without raising network errors."""
        profiles: list[dict[str, Any]] = []
        activities: list[KillActivity] = []
        for character_id in _unique_positive_ints(observation.character_ids):
            profile = self.character_profile(character_id)
            if profile is not None:
                profiles.append(profile)

            activity = self.kill_activity(character_id)
            if activity is not None:
                activities.append(activity)

        return ThreatEnrichment(
            character_profiles=profiles,
            kill_activities=activities,
        )

    def character_profile(self, character_id: int) -> dict[str, Any] | None:
        """Return a cached public ESI character profile when available."""
        if self.resolver is None or not hasattr(self.resolver, "character_profile"):
            return None
        try:
            profile = self.resolver.character_profile(int(character_id))
        except Exception:
            return None
        return profile if isinstance(profile, dict) else None

    def system_profile(self, system_id: int) -> dict[str, Any] | None:
        """Return a cached public ESI solar-system profile when available."""
        if self.resolver is None or not hasattr(self.resolver, "system_profile"):
            return None
        try:
            profile = self.resolver.system_profile(int(system_id))
        except Exception:
            return None
        return profile if isinstance(profile, dict) else None

    def kill_activity(self, character_id: int) -> KillActivity | None:
        """Return recent zKillboard activity for one character when available."""
        if self.killboard is None or not hasattr(self.killboard, "character_recent"):
            return None
        try:
            rows = self.killboard.character_recent(int(character_id))
            if not isinstance(rows, list):
                return None
            return analyze_character_activity(
                int(character_id),
                rows,
                window=self.kill_window,
            )
        except Exception:
            return None

    def system_kill_activity(self, system_id: int) -> SystemKillActivity | None:
        """Return recent zKillboard activity for one solar system when available."""
        if self.killboard is None or not hasattr(self.killboard, "system_recent"):
            return None
        try:
            rows = self.killboard.system_recent(int(system_id))
            if not isinstance(rows, list):
                return None
            return analyze_system_activity(
                int(system_id),
                rows,
                window=self.kill_window,
            )
        except Exception:
            return None

    def corporation_kill_activity(
        self,
        corporation_id: int,
    ) -> GroupKillActivity | None:
        """Return recent zKillboard activity for one corporation when available."""
        return self._group_kill_activity(
            corporation_id,
            entity_type="corporation",
            method_name="corporation_recent",
        )

    def alliance_kill_activity(self, alliance_id: int) -> GroupKillActivity | None:
        """Return recent zKillboard activity for one alliance when available."""
        return self._group_kill_activity(
            alliance_id,
            entity_type="alliance",
            method_name="alliance_recent",
        )

    def _group_kill_activity(
        self,
        entity_id: int,
        entity_type: str,
        method_name: str,
    ) -> GroupKillActivity | None:
        if self.killboard is None or not hasattr(self.killboard, method_name):
            return None
        try:
            rows = getattr(self.killboard, method_name)(int(entity_id))
            if not isinstance(rows, list):
                return None
            return analyze_group_activity(
                int(entity_id),
                rows,
                entity_type=entity_type,
                window=self.kill_window,
            )
        except Exception:
            return None


def _unique_positive_ints(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.add(number)
            result.append(number)
    return result
