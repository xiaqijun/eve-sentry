"""Optional ESI and killboard enrichment for threat scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable

from app.core.models import Observation
from app.esi.session import (
    ContactStanding,
    apply_contact_standing,
    contact_standings_from_payload,
)
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
    group_activities: list[GroupKillActivity] = field(default_factory=list)

    def has_data(self) -> bool:
        """Return whether any enrichment source produced usable data."""
        return bool(
            self.character_profiles
            or self.kill_activities
            or self.group_activities
        )


class ThreatEnricher:
    """Collect public ESI profiles and killboard activity for scoring."""

    def __init__(
        self,
        resolver: Any | None = None,
        killboard: Any | None = None,
        esi_session: Any | None = None,
        kill_window: str = "recent",
        standing_ttl_seconds: float = 300.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.resolver = resolver
        self.killboard = killboard
        self.esi_session = esi_session
        self.kill_window = kill_window
        self.standing_ttl_seconds = max(0.0, float(standing_ttl_seconds))
        self._now = now or time
        self._contact_standings: list[ContactStanding] | None = None
        self._contact_standings_until = 0.0

    def enrich(self, observation: Observation) -> ThreatEnrichment:
        """Return best-effort enrichment without raising network errors."""
        profiles: list[dict[str, Any]] = []
        activities: list[KillActivity] = []
        corporation_ids: set[int] = set()
        alliance_ids: set[int] = set()
        contacts = self.contact_standings()
        for character_id in _unique_positive_ints(observation.character_ids):
            profile = self._public_character_profile(character_id)
            if contacts:
                base_profile = profile or {"character_id": character_id}
                annotated = apply_contact_standing(base_profile, contacts)
                if profile is not None or "contact_standing" in annotated:
                    profile = annotated
            if profile is not None:
                profiles.append(profile)
                _add_profile_entity_ids(profile, corporation_ids, alliance_ids)

            activity = self.kill_activity(character_id)
            if activity is not None:
                activities.append(activity)

        return ThreatEnrichment(
            character_profiles=profiles,
            kill_activities=activities,
            group_activities=self._group_activities(corporation_ids, alliance_ids),
        )

    def character_profile(self, character_id: int) -> dict[str, Any] | None:
        """Return a cached public ESI character profile when available."""
        profile = self._public_character_profile(character_id)
        if profile is None:
            return None
        contacts = self.contact_standings()
        if contacts:
            return apply_contact_standing(profile, contacts)
        return profile

    def contact_standings(self) -> list[ContactStanding]:
        """Return cached authenticated contact standings when configured."""
        if self.esi_session is None or not hasattr(self.esi_session, "snapshot"):
            return []

        now = float(self._now())
        if (
            self._contact_standings is not None
            and now < self._contact_standings_until
        ):
            return list(self._contact_standings)

        try:
            snapshot = self.esi_session.snapshot(
                include_location=False,
                include_contacts=True,
            )
        except Exception:
            return []

        contacts = _normalize_contact_standings(getattr(snapshot, "contacts", []))
        self._contact_standings = contacts
        self._contact_standings_until = now + self.standing_ttl_seconds
        return list(contacts)

    def _public_character_profile(self, character_id: int) -> dict[str, Any] | None:
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

    def _group_activities(
        self,
        corporation_ids: set[int],
        alliance_ids: set[int],
    ) -> list[GroupKillActivity]:
        activities = []
        for corporation_id in sorted(corporation_ids):
            activity = self.corporation_kill_activity(corporation_id)
            if activity is not None:
                activities.append(activity)
        for alliance_id in sorted(alliance_ids):
            activity = self.alliance_kill_activity(alliance_id)
            if activity is not None:
                activities.append(activity)
        return activities


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


def _add_profile_entity_ids(
    profile: dict[str, Any],
    corporation_ids: set[int],
    alliance_ids: set[int],
) -> None:
    corporation_id = _optional_positive_int(profile.get("corporation_id"))
    if corporation_id is not None:
        corporation_ids.add(corporation_id)
    alliance_id = _optional_positive_int(profile.get("alliance_id"))
    if alliance_id is not None:
        alliance_ids.add(alliance_id)


def _optional_positive_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_contact_standings(value: Any) -> list[ContactStanding]:
    if not isinstance(value, list):
        return []
    contacts: list[ContactStanding] = []
    dict_rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, ContactStanding):
            contacts.append(item)
        elif isinstance(item, dict):
            dict_rows.append(item)
    contacts.extend(contact_standings_from_payload(dict_rows))
    return contacts
