"""Multi-source threat scoring for observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable

from app.core.models import Evidence, Observation, ThreatEvent, threat_level
from app.intel.evidence import make_evidence
from app.killboard.analyzer import (
    GroupKillActivity,
    KillActivity,
    activity_score_bonus,
    group_activity_score_bonus,
)


@dataclass(frozen=True)
class Watchlist:
    """User-controlled scoring lists."""

    whitelist: set[str] = field(default_factory=set)
    blacklist: set[str] = field(default_factory=set)
    hostile_corporation_ids: set[int] = field(default_factory=set)
    hostile_alliance_ids: set[int] = field(default_factory=set)
    hostile_standing_threshold: float | None = -5.0


@dataclass(frozen=True)
class ChannelMention:
    """Recent intel-channel context near the scored observation."""

    observation: Observation
    relation: str
    age_seconds: float | None = None


class ScoringEngine:
    """Generate threat events from observations and optional enrichment."""

    def __init__(
        self,
        watchlist: Watchlist | None = None,
        cooldown_seconds: float = 60.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.watchlist = watchlist or Watchlist()
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._now = now or time
        self._last_alert_at: dict[str, float] = {}

    def score(
        self,
        observation: Observation,
        kill_activity: KillActivity | None = None,
        character_profile: dict[str, Any] | None = None,
        kill_activities: list[KillActivity] | None = None,
        character_profiles: list[dict[str, Any]] | None = None,
        group_activity: GroupKillActivity | None = None,
        group_activities: list[GroupKillActivity] | None = None,
        channel_mentions: list[ChannelMention] | None = None,
    ) -> ThreatEvent | None:
        """Return a threat event, or None when suppressed by rules/cooldown."""
        names = self._event_names(observation)
        if self._is_whitelisted(names):
            return None

        cooldown_key = self._cooldown_key(observation, names)
        if self._is_in_cooldown(cooldown_key):
            return None

        evidence: list[Evidence] = []
        evidence.extend(self._source_evidence(observation, names))
        evidence.extend(self._watchlist_evidence(names))
        for profile in self._profile_inputs(character_profile, character_profiles):
            evidence.extend(self._profile_evidence(profile))
        for activity in self._activity_inputs(kill_activity, kill_activities):
            evidence.extend(self._kill_activity_evidence(activity))
        for activity in self._group_activity_inputs(group_activity, group_activities):
            evidence.extend(self._group_activity_evidence(activity))
        evidence.extend(self._channel_mention_evidence(channel_mentions))

        score = sum(item.weight for item in evidence)
        if score <= 0:
            return None

        self._last_alert_at[cooldown_key] = self._now()
        return ThreatEvent(
            event_id=f"evt_{observation.observation_id}",
            system_name=observation.system_name,
            system_id=observation.system_id,
            names=names,
            character_ids=list(observation.character_ids),
            score=score,
            level=threat_level(score),
            evidence=evidence,
            source_observation_id=observation.observation_id,
            created_at=observation.received_at,
        )

    def _source_evidence(
        self,
        observation: Observation,
        names: list[str],
    ) -> list[Evidence]:
        label = ", ".join(names)
        raw_source = observation.source.strip()
        source = raw_source.casefold()
        weight = self._source_weight(observation, source)
        if weight <= 0:
            return []
        if source in {"local_ocr", "ocr", "eve-sentry-detector"}:
            return [
                make_evidence(
                    "local_ocr_seen",
                    weight,
                    f"Local OCR saw {label} in {observation.system_name}",
                )
            ]
        if source == "intel_channel":
            return [
                make_evidence(
                    "intel_channel_report",
                    weight,
                    self._intel_channel_summary(observation, label),
                )
            ]
        if source == "manual":
            return [
                make_evidence(
                    "manual_intel",
                    weight,
                    f"Manual intel reported {label} in {observation.system_name}",
                )
            ]
        if source == "killboard":
            return [
                make_evidence(
                    "killboard_observed",
                    weight,
                    f"Killboard activity references {label}",
                )
            ]
        return [
            make_evidence(
                "generic_observation",
                weight,
                f"{raw_source or 'Intel source'} reported {label}",
            )
        ]

    def _source_weight(self, observation: Observation, source: str) -> int:
        if self._is_unknown_system(observation):
            return 0
        if source in {"local_ocr", "ocr", "eve-sentry-detector"}:
            if not self._has_character_target(observation):
                return 0
            return self._confidence_adjusted_weight(40, observation)
        if source == "intel_channel":
            if not self._has_intel_target(observation):
                return 0
            return self._confidence_adjusted_weight(30, observation)
        if source == "manual":
            return 50
        if source == "killboard":
            return 20
        return self._confidence_adjusted_weight(25, observation)

    def _confidence_adjusted_weight(
        self,
        base_weight: int,
        observation: Observation,
    ) -> int:
        confidence = _normalized_confidence(observation.confidence)
        if confidence is None or confidence >= 0.75:
            return base_weight
        if confidence >= 0.5:
            return _scale_weight(base_weight, 0.75)
        if confidence >= 0.25:
            return _scale_weight(base_weight, 0.5)
        if self._has_identity_support(observation):
            return _scale_weight(base_weight, 0.5)
        return 0

    def _has_character_target(self, observation: Observation) -> bool:
        return bool(observation.names or observation.character_ids)

    def _has_intel_target(self, observation: Observation) -> bool:
        return bool(
            observation.names
            or observation.character_ids
            or _optional_int(observation.metadata.get("hostile_count")) is not None
        )

    def _has_identity_support(self, observation: Observation) -> bool:
        return bool(
            observation.character_ids
            or _optional_int(observation.metadata.get("hostile_count")) is not None
        )

    def _is_unknown_system(self, observation: Observation) -> bool:
        return observation.system_name.strip().casefold() == "unknown"

    def _watchlist_evidence(self, names: list[str]) -> list[Evidence]:
        evidence = []
        blacklisted = [
            name for name in names if name.casefold() in self._blacklist_names()
        ]
        if blacklisted:
            evidence.append(
                make_evidence(
                    "blacklist_match",
                    80,
                    f"Blacklisted pilot: {', '.join(blacklisted)}",
                )
            )
        return evidence

    def _intel_channel_summary(self, observation: Observation, label: str) -> str:
        hostile_count = _optional_int(observation.metadata.get("hostile_count"))
        jump_count = _optional_int(observation.metadata.get("jump_count"))
        direction = _clean_meta_string(observation.metadata.get("direction"))
        if hostile_count is not None and not observation.names:
            target = f"{hostile_count} hostile"
            if hostile_count != 1:
                target += "s"
        else:
            target = label

        summary = f"Intel channel reported {target} in {observation.system_name}"
        if direction:
            summary = f"{summary} toward {direction}"
        if jump_count is not None:
            suffix = "jump" if jump_count == 1 else "jumps"
            summary = f"{summary} ({jump_count} {suffix})"
        return summary

    def _profile_evidence(
        self,
        character_profile: dict[str, Any] | None,
    ) -> list[Evidence]:
        if not character_profile:
            return []

        evidence = []
        corporation_id = _optional_int(character_profile.get("corporation_id"))
        alliance_id = _optional_int(character_profile.get("alliance_id"))
        standing = _optional_float(character_profile.get("standing"))
        if standing is None:
            standing = _optional_float(character_profile.get("contact_standing"))
        if corporation_id in self.watchlist.hostile_corporation_ids:
            evidence.append(
                make_evidence(
                    "hostile_corporation",
                    60,
                    f"Hostile corporation id {corporation_id}",
                )
            )
        if alliance_id in self.watchlist.hostile_alliance_ids:
            evidence.append(
                make_evidence(
                    "hostile_alliance",
                    60,
                    f"Hostile alliance id {alliance_id}",
                )
            )
        if (
            standing is not None
            and self.watchlist.hostile_standing_threshold is not None
            and standing <= self.watchlist.hostile_standing_threshold
        ):
            evidence.append(
                make_evidence(
                    "hostile_standing",
                    70,
                    f"Hostile standing {standing:g}",
                )
            )
        return evidence

    def _kill_activity_evidence(self, activity: KillActivity) -> list[Evidence]:
        bonus = activity_score_bonus(activity)
        if bonus <= 0:
            return []
        return [
            make_evidence(
                "recent_kill_activity",
                bonus,
                f"{activity.kills} recent kills from zKillboard",
            )
        ]

    def _group_activity_evidence(
        self,
        activity: GroupKillActivity,
    ) -> list[Evidence]:
        bonus = group_activity_score_bonus(activity)
        if bonus <= 0:
            return []
        label = activity.entity_type.replace("_", " ").title()
        summary = (
            f"{label} {activity.entity_id} has {activity.kills} recent "
            "kills from zKillboard"
        )
        if activity.losses:
            summary = f"{summary} and {activity.losses} losses"
        return [
            make_evidence(
                f"{activity.entity_type}_kill_activity",
                bonus,
                summary,
            )
        ]

    def _channel_mention_evidence(
        self,
        mentions: list[ChannelMention] | None,
    ) -> list[Evidence]:
        if not mentions:
            return []

        latest_by_relation: dict[str, ChannelMention] = {}
        for mention in mentions:
            relation = mention.relation.strip().casefold()
            if relation not in {"same_system", "adjacent_system"}:
                continue
            existing = latest_by_relation.get(relation)
            if existing is None or _time_key(mention) > _time_key(existing):
                latest_by_relation[relation] = mention

        evidence = []
        same_system = latest_by_relation.get("same_system")
        if same_system is not None:
            evidence.append(
                make_evidence(
                    "intel_channel_same_system_recent",
                    30,
                    self._channel_mention_summary(same_system, adjacent=False),
                )
            )
        adjacent = latest_by_relation.get("adjacent_system")
        if adjacent is not None:
            evidence.append(
                make_evidence(
                    "intel_channel_adjacent_system_recent",
                    15,
                    self._channel_mention_summary(adjacent, adjacent=True),
                )
            )
        return evidence

    def _channel_mention_summary(
        self,
        mention: ChannelMention,
        adjacent: bool,
    ) -> str:
        observation = mention.observation
        location = (
            f"adjacent system {observation.system_name}"
            if adjacent
            else observation.system_name
        )
        age = _age_label(mention.age_seconds)
        prefix = "Recent intel channel mention"
        if age:
            prefix = f"{prefix} {age}"
        return f"{prefix} in {location}"

    def _profile_inputs(
        self,
        character_profile: dict[str, Any] | None,
        character_profiles: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        profiles = []
        if character_profile:
            profiles.append(character_profile)
        profiles.extend(item for item in character_profiles or [] if item)
        return profiles

    def _activity_inputs(
        self,
        kill_activity: KillActivity | None,
        kill_activities: list[KillActivity] | None,
    ) -> list[KillActivity]:
        activities = []
        if kill_activity is not None:
            activities.append(kill_activity)
        activities.extend(item for item in kill_activities or [] if item is not None)
        return activities

    def _group_activity_inputs(
        self,
        group_activity: GroupKillActivity | None,
        group_activities: list[GroupKillActivity] | None,
    ) -> list[GroupKillActivity]:
        activities = []
        if group_activity is not None:
            activities.append(group_activity)
        activities.extend(item for item in group_activities or [] if item is not None)
        return activities

    def _event_names(self, observation: Observation) -> list[str]:
        if observation.names:
            return list(observation.names)
        if observation.character_ids:
            return [str(item) for item in observation.character_ids]
        return [observation.raw_text or "Unknown target"]

    def _is_whitelisted(self, names: list[str]) -> bool:
        whitelist = {name.casefold() for name in self.watchlist.whitelist}
        if not whitelist:
            return False
        return all(name.casefold() in whitelist for name in names)

    def _blacklist_names(self) -> set[str]:
        return {name.casefold() for name in self.watchlist.blacklist}

    def _cooldown_key(self, observation: Observation, names: list[str]) -> str:
        joined_names = ",".join(sorted(name.casefold() for name in names))
        return f"{observation.system_name.casefold()}:{joined_names}"

    def _is_in_cooldown(self, key: str) -> bool:
        if self.cooldown_seconds <= 0:
            return False
        last_alert_at = self._last_alert_at.get(key)
        if last_alert_at is None:
            return False
        return self._now() - last_alert_at < self.cooldown_seconds


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_meta_string(value: Any) -> str:
    return str(value or "").strip()


def _normalized_confidence(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, confidence))


def _scale_weight(value: int, factor: float) -> int:
    return max(0, int(float(value) * float(factor) + 0.5))


def _time_key(mention: ChannelMention) -> str:
    return mention.observation.seen_at or mention.observation.received_at


def _age_label(age_seconds: float | None) -> str:
    if age_seconds is None or age_seconds < 0:
        return ""
    minutes = int(age_seconds // 60)
    if minutes <= 0:
        return "just now"
    if minutes == 1:
        return "1 minute ago"
    return f"{minutes} minutes ago"
