"""Multi-source threat scoring for observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable

from app.core.models import Evidence, Observation, ThreatEvent, threat_level
from app.intel.evidence import make_evidence


SCORING_VERSION = "scoring.v1"


@dataclass(frozen=True)
class Watchlist:
    """User-controlled scoring lists."""

    whitelist: set[str] = field(default_factory=set)
    blacklist: set[str] = field(default_factory=set)
    friendly_corporation_ids: set[int] = field(default_factory=set)
    friendly_alliance_ids: set[int] = field(default_factory=set)
    hostile_corporation_ids: set[int] = field(default_factory=set)
    hostile_alliance_ids: set[int] = field(default_factory=set)
    friendly_standing_threshold: float | None = 5.0
    hostile_standing_threshold: float | None = 0.0


@dataclass(frozen=True)
class ChannelMention:
    """Recent intel-channel context near the scored observation."""

    observation: Observation
    relation: str
    age_seconds: float | None = None


class ScoringEngine:
    """Generate threat events from observations and optional enrichment."""

    scoring_version = SCORING_VERSION

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
        kill_activity: Any | None = None,
        character_profile: dict[str, Any] | None = None,
        kill_activities: list[Any] | None = None,
        character_profiles: list[dict[str, Any]] | None = None,
        group_activity: Any | None = None,
        group_activities: list[Any] | None = None,
        channel_mentions: list[ChannelMention] | None = None,
    ) -> ThreatEvent | None:
        """Return a threat event, or None when suppressed by rules/cooldown."""
        names = self._event_names(observation)
        profiles = self._profile_inputs(character_profile, character_profiles)
        if self.suppresses_observation(observation, names, profiles):
            return None

        cooldown_key = self._cooldown_key(observation, names)
        if self._is_in_cooldown(cooldown_key):
            return None

        evidence: list[Evidence] = []
        evidence.extend(self._source_evidence(observation, names))
        evidence.extend(self._watchlist_evidence(names))
        for profile in profiles:
            evidence.extend(self._profile_evidence(profile))
        evidence.extend(self._channel_mention_evidence(channel_mentions))

        if self._is_local_ocr_observation(observation) and not self._has_hostile_evidence(
            evidence
        ):
            return None

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
            scoring_version=self.scoring_version,
        )

    def reset_cooldown(self, system_name: str, names: list[str]) -> bool:
        """Clear one target's cooldown after a confirmed departure."""
        normalized_names = [
            str(name).strip()
            for name in names
            if str(name).strip()
        ]
        if not normalized_names:
            return False
        key = f"{str(system_name).strip().casefold()}:" + ",".join(
            sorted(name.casefold() for name in normalized_names)
        )
        return self._last_alert_at.pop(key, None) is not None

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
            weight = self._confidence_adjusted_weight(40, observation)
            return self._resolution_adjusted_weight(weight, observation)
        if source == "intel_channel":
            if not self._has_intel_target(observation):
                return 0
            weight = self._confidence_adjusted_weight(30, observation)
            return self._resolution_adjusted_weight(weight, observation)
        if source == "manual":
            return 50
        weight = self._confidence_adjusted_weight(25, observation)
        return self._resolution_adjusted_weight(weight, observation)

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

    def _is_local_ocr_observation(self, observation: Observation) -> bool:
        return observation.source.strip().casefold() in {
            "local_ocr",
            "ocr",
            "eve-sentry-detector",
        }

    def _has_hostile_evidence(self, evidence: list[Evidence]) -> bool:
        hostile_types = {
            "blacklist_match",
            "hostile_corporation",
            "hostile_alliance",
            "hostile_standing",
        }
        return any(item.evidence_type in hostile_types for item in evidence)

    def _is_unknown_system(self, observation: Observation) -> bool:
        return observation.system_name.strip().casefold() == "unknown"

    def _resolution_adjusted_weight(
        self,
        weight: int,
        observation: Observation,
    ) -> int:
        if weight <= 0:
            return 0
        resolution = _esi_resolution(observation.metadata.get("esi_resolution"))
        if resolution is None or not _resolution_attempted(resolution):
            if _resolution_status(resolution) in {"ambiguous", "no_match"}:
                return 0
            return weight
        if not _resolution_system_name_matched(resolution):
            return 0
        if (
            observation.names
            and not observation.character_ids
            and _resolution_name_count(resolution, observation) > 0
            and _resolution_resolved_character_count(resolution) <= 0
        ):
            return 0
        return weight

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

    def suppresses_observation(
        self,
        observation: Observation,
        names: list[str] | None = None,
        character_profiles: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Return true when a target is fully covered by friendly watchlists."""
        event_names = self._event_names(observation) if names is None else list(names)
        if self._all_names_whitelisted(event_names):
            return True
        return self._all_targets_have_friendly_profiles(
            observation,
            character_profiles or [],
        )

    def _event_names(self, observation: Observation) -> list[str]:
        if observation.names:
            return list(observation.names)
        if observation.character_ids:
            return [str(item) for item in observation.character_ids]
        return [observation.raw_text or "Unknown target"]

    def _all_names_whitelisted(self, names: list[str]) -> bool:
        whitelist = {
            key
            for name in self.watchlist.whitelist
            for key in _ocr_name_match_keys(name)
        }
        if not whitelist:
            return False
        return all(
            bool(_ocr_name_match_keys(name) & whitelist)
            for name in names
        )

    def _all_targets_have_friendly_profiles(
        self,
        observation: Observation,
        profiles: list[dict[str, Any]],
    ) -> bool:
        if not profiles:
            return False
        friendly_profiles = [
            profile for profile in profiles if self._is_friendly_profile(profile)
        ]
        if not friendly_profiles:
            return False
        target_count = max(
            len(observation.names),
            len(observation.character_ids),
            len(profiles),
        )
        return target_count > 0 and len(friendly_profiles) >= target_count

    def _is_friendly_profile(self, profile: dict[str, Any]) -> bool:
        corporation_id = _optional_int(profile.get("corporation_id"))
        alliance_id = _optional_int(profile.get("alliance_id"))
        standing = _optional_float(profile.get("contact_standing"))
        if standing is None:
            standing = _optional_float(profile.get("standing"))
        return (
            corporation_id in self.watchlist.friendly_corporation_ids
            or alliance_id in self.watchlist.friendly_alliance_ids
            or (
                standing is not None
                and self.watchlist.friendly_standing_threshold is not None
                and standing >= self.watchlist.friendly_standing_threshold
            )
        )

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


def _ocr_name_match_keys(name: str) -> set[str]:
    text = str(name or "").strip().casefold()
    if not text:
        return set()
    keys = {text}
    first = text[0]
    if first == "l":
        keys.add(f"i{text[1:]}")
    elif first == "i":
        keys.add(f"l{text[1:]}")
    return keys


def _clean_meta_string(value: Any) -> str:
    return str(value or "").strip()


def _esi_resolution(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _resolution_attempted(value: dict[str, Any]) -> bool:
    return bool(value.get("attempted"))


def _resolution_status(value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("system_repair_status") or "").strip().casefold()


def _resolution_system_name_matched(value: dict[str, Any]) -> bool:
    matched = value.get("system_name_matched")
    return True if matched is None else bool(matched)


def _resolution_name_count(
    value: dict[str, Any],
    observation: Observation,
) -> int:
    raw = value.get("character_name_count")
    if raw in {None, ""}:
        return len(observation.names)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return len(observation.names)


def _resolution_resolved_character_count(value: dict[str, Any]) -> int:
    raw = value.get("resolved_character_count")
    if raw in {None, ""}:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


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
