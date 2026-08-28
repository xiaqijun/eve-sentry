"""Classification-first alerting for server-side intel observations."""

from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any, Callable

from app.core.models import Evidence, Observation, ThreatEvent
from app.intel.scoring import ChannelMention, Watchlist


CLASSIFICATION_VERSION = "classification.v1"


@dataclass(frozen=True)
class ClassificationResult:
    """A single server-side classification outcome."""

    classification: str
    reason: str
    evidence: list[Evidence]


class ClassificationEngine:
    """Generate one alert when an observation matches friendly or hostile intel."""

    scoring_version = CLASSIFICATION_VERSION
    suppress_whitelisted_reports = False

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
        """Return a classification alert, or None when the target is unknown."""
        del kill_activity, kill_activities, group_activity, group_activities
        names = self._event_names(observation)
        profiles = self._profile_inputs(character_profile, character_profiles)
        result = self.classify(observation, names, profiles)
        if result is None:
            return None

        cooldown_key = self._cooldown_key(observation, names, result.classification)
        if self._is_in_cooldown(cooldown_key):
            return None

        evidence = list(result.evidence)
        evidence.extend(self._source_context_evidence(observation, names))
        evidence.extend(self._channel_context_evidence(channel_mentions))
        score, level = self._compat_score_level(result.classification)
        self._last_alert_at[cooldown_key] = self._now()
        return ThreatEvent(
            event_id=f"evt_{observation.observation_id}",
            system_name=observation.system_name,
            system_id=observation.system_id,
            names=names,
            character_ids=list(observation.character_ids),
            score=score,
            level=level,
            evidence=evidence,
            source_observation_id=observation.observation_id,
            created_at=observation.received_at,
            scoring_version=self.scoring_version,
            classification=result.classification,
            reason=result.reason,
        )

    def classify(
        self,
        observation: Observation,
        names: list[str] | None = None,
        character_profiles: list[dict[str, Any]] | None = None,
    ) -> ClassificationResult | None:
        """Classify an observation as hostile, friendly, or unknown."""
        event_names = self._event_names_from_names(names or [])
        profiles = character_profiles or []

        hostile = self._hostile_evidence(event_names, profiles)
        friendly = self._friendly_evidence(event_names, profiles)
        # Detector icon counts describe the whole screenshot, not each OCR
        # identity. Once ESI has confirmed a friendly identity, do not attach
        # that shared visual evidence to the friendly pilot.
        if friendly and not hostile:
            return ClassificationResult(
                classification="white",
                reason=friendly[0].summary,
                evidence=friendly,
            )
        hostile_icon_count = _optional_int(
            observation.metadata.get("hostile_icon_count")
        )
        if (
            observation.source.strip().casefold()
            in {"local_ocr", "ocr", "eve-sentry-detector"}
            and hostile_icon_count is not None
            and hostile_icon_count > 0
        ):
            hostile.insert(
                0,
                Evidence(
                    "hostile_icon",
                    100,
                    f"Client detected {hostile_icon_count} red standing icon(s) in "
                    f"{observation.system_name}",
                ),
            )
        if hostile:
            return ClassificationResult(
                classification="red",
                reason=hostile[0].summary,
                evidence=hostile,
            )

        if friendly:
            return ClassificationResult(
                classification="white",
                reason=friendly[0].summary,
                evidence=friendly,
            )
        return None

    def suppresses_observation(
        self,
        observation: Observation,
        names: list[str] | None = None,
        character_profiles: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Classification does not hide observations; it annotates them."""
        del observation, names, character_profiles
        return False

    def _hostile_evidence(
        self,
        names: list[str],
        profiles: list[dict[str, Any]],
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        blacklist = self._blacklist_names()
        for name in names:
            if name.casefold() in blacklist:
                evidence.append(
                    Evidence("hostile_name", 100, f"Hostile pilot name {name}")
                )
        for profile in profiles:
            evidence.extend(self._profile_evidence(profile, hostile=True))
        return evidence

    def _friendly_evidence(
        self,
        names: list[str],
        profiles: list[dict[str, Any]],
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        whitelist = self._whitelist_match_keys()
        for name in names:
            if _ocr_name_match_keys(name) & whitelist:
                evidence.append(
                    Evidence("friendly_name", 1, f"Friendly pilot name {name}")
                )
        for profile in profiles:
            evidence.extend(self._profile_evidence(profile, hostile=False))
        return evidence

    def _profile_evidence(
        self,
        profile: dict[str, Any],
        hostile: bool,
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        corporation_id = _optional_int(profile.get("corporation_id"))
        alliance_id = _optional_int(profile.get("alliance_id"))
        standing = _optional_float(profile.get("contact_standing"))
        if standing is None:
            standing = _optional_float(profile.get("standing"))

        if hostile:
            if corporation_id in self.watchlist.hostile_corporation_ids:
                evidence.append(
                    Evidence(
                        "hostile_corporation",
                        100,
                        f"Hostile corporation id {corporation_id}",
                    )
                )
            if alliance_id in self.watchlist.hostile_alliance_ids:
                evidence.append(
                    Evidence(
                        "hostile_alliance",
                        100,
                        f"Hostile alliance id {alliance_id}",
                    )
                )
            if (
                standing is not None
                and self.watchlist.hostile_standing_threshold is not None
                and standing <= self.watchlist.hostile_standing_threshold
            ):
                evidence.append(
                    Evidence("hostile_standing", 100, f"Hostile standing {standing:g}")
                )
            return evidence

        if corporation_id in self.watchlist.friendly_corporation_ids:
            evidence.append(
                Evidence(
                    "friendly_corporation",
                    1,
                    f"Friendly corporation id {corporation_id}",
                )
            )
        if alliance_id in self.watchlist.friendly_alliance_ids:
            evidence.append(
                Evidence("friendly_alliance", 1, f"Friendly alliance id {alliance_id}")
            )
        if (
            standing is not None
            and self.watchlist.friendly_standing_threshold is not None
            and standing >= self.watchlist.friendly_standing_threshold
        ):
            evidence.append(
                Evidence("friendly_standing", 1, f"Friendly standing {standing:g}")
            )
        return evidence

    def _source_context_evidence(
        self,
        observation: Observation,
        names: list[str],
    ) -> list[Evidence]:
        label = ", ".join(names)
        source = observation.source.strip() or "api"
        if not label:
            return []
        return [
            Evidence(
                "observation_context",
                0,
                f"{source} observed {label} in {observation.system_name}",
            )
        ]

    def _channel_context_evidence(
        self,
        mentions: list[ChannelMention] | None,
    ) -> list[Evidence]:
        if not mentions:
            return []
        latest = max(mentions, key=lambda item: item.age_seconds or 0)
        relation = latest.relation.replace("_", " ")
        return [
            Evidence(
                "intel_channel_context",
                0,
                f"Recent {relation} channel mention near {latest.observation.system_name}",
            )
        ]

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

    def _event_names(self, observation: Observation) -> list[str]:
        if observation.names:
            return list(observation.names)
        if observation.character_ids:
            return [str(item) for item in observation.character_ids]
        return [observation.raw_text or "Unknown target"]

    def _event_names_from_names(self, names: list[str]) -> list[str]:
        return [name for name in names if str(name).strip()]

    def _blacklist_names(self) -> set[str]:
        return {name.casefold() for name in self.watchlist.blacklist}

    def _whitelist_match_keys(self) -> set[str]:
        return {
            key
            for name in self.watchlist.whitelist
            for key in _ocr_name_match_keys(name)
        }

    def _cooldown_key(
        self,
        observation: Observation,
        names: list[str],
        classification: str,
    ) -> str:
        joined_names = ",".join(sorted(name.casefold() for name in names))
        return f"{classification}:{observation.system_name.casefold()}:{joined_names}"

    def _is_in_cooldown(self, key: str) -> bool:
        if self.cooldown_seconds <= 0:
            return False
        last_alert_at = self._last_alert_at.get(key)
        if last_alert_at is None:
            return False
        return self._now() - last_alert_at < self.cooldown_seconds

    def _compat_score_level(self, classification: str) -> tuple[int, str]:
        if classification == "red":
            return 100, "critical"
        return 1, "low"


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
