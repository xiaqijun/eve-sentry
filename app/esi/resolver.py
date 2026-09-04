"""Resolve and enrich intel observations with public ESI data."""

from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass
from time import monotonic
from typing import Any

from app.core.models import Observation
from app.esi.cache import EsiCache
from app.esi.client import EsiApiError, EsiClient


@dataclass(frozen=True)
class ResolvedName:
    """A name resolved by ESI."""

    name: str
    category: str
    entity_id: int


class EsiResolver:
    """Public ESI resolver with local caching."""

    def __init__(
        self,
        client: EsiClient | Any | None = None,
        cache: EsiCache | None = None,
        ttl_seconds: int = 86400,
        profile_ttl_seconds: int = 21600,
        affiliation_ttl_seconds: int = 3600,
        negative_ttl_seconds: int = 600,
    ) -> None:
        self.client = client or EsiClient()
        self.cache = cache or EsiCache()
        self.ttl_seconds = ttl_seconds
        self.profile_ttl_seconds = max(1, int(profile_ttl_seconds))
        self.affiliation_ttl_seconds = max(1, int(affiliation_ttl_seconds))
        self.negative_ttl_seconds = max(1, int(negative_ttl_seconds))
        self._resolve_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._personnel_cache: Counter[str] = Counter()
        self._personnel_lookup_times: deque[float] = deque(maxlen=10000)

    def resolve_names(self, names: list[str]) -> list[ResolvedName]:
        """Resolve names to ids, preserving the input order where possible."""
        # OCR snapshots from several clients commonly contain the same pilots.
        # Serialize cache misses so only one batch reaches ESI.
        with self._resolve_lock:
            return self._resolve_names_locked(names)

    def cached_name(
        self,
        name: str,
        *,
        allow_stale: bool = False,
    ) -> tuple[ResolvedName | None, str]:
        """Return one cached name resolution without performing network I/O."""
        text = str(name or "").strip()
        if not text:
            return None, "miss"
        key = self._name_key(text)
        value = self.cache.get(key)
        status = str(self.cache.metadata(key).get("cache_status") or "miss")
        if value is None and allow_stale:
            value = self.cache.get_stale(key)
        if not isinstance(value, dict):
            self._record_personnel_cache_lookup("miss")
            return None, "miss"
        if value.get("status") == "not_found":
            self._record_personnel_cache_lookup(
                "stale_negative" if status == "stale" else "negative"
            )
            return None, f"{status}_not_found"
        try:
            resolved = ResolvedName(
                name=str(value["name"]),
                category=str(value["category"]),
                entity_id=int(value["id"]),
            )
        except (KeyError, TypeError, ValueError):
            self._record_personnel_cache_lookup("miss")
            return None, "miss"
        self._record_personnel_cache_lookup(
            "stale" if status == "stale" else "fresh"
        )
        return resolved, status

    def cache_snapshot(self) -> dict[str, Any]:
        """Return business cache metrics with OCR personnel lookup semantics."""
        snapshot = getattr(self.cache, "snapshot", None)
        payload = snapshot() if callable(snapshot) else {}
        now = monotonic()
        with self._metrics_lock:
            lookups = self._personnel_cache["lookups"]
            hits = self._personnel_cache["hits"]
            personnel = {
                "lookups": lookups,
                "hits": hits,
                "misses": self._personnel_cache["misses"],
                "fresh_hits": self._personnel_cache["fresh_hits"],
                "stale_hits": self._personnel_cache["stale_hits"],
                "negative_hits": self._personnel_cache["negative_hits"],
                "hit_rate": round(hits / max(1, lookups), 4),
                "lookup_rate_per_second": round(
                    sum(
                        timestamp >= now - 60.0
                        for timestamp in self._personnel_lookup_times
                    )
                    / 60.0,
                    4,
                ),
            }
        return {**payload, "personnel": personnel}

    def _record_personnel_cache_lookup(self, result: str) -> None:
        with self._metrics_lock:
            self._personnel_cache["lookups"] += 1
            if result == "miss":
                self._personnel_cache["misses"] += 1
            else:
                self._personnel_cache["hits"] += 1
                if result == "fresh":
                    self._personnel_cache["fresh_hits"] += 1
                elif result in {"stale", "stale_negative"}:
                    self._personnel_cache["stale_hits"] += 1
                if result in {"negative", "stale_negative"}:
                    self._personnel_cache["negative_hits"] += 1
            self._personnel_lookup_times.append(monotonic())

    def cached_character_profile(
        self,
        character_id: int,
        *,
        allow_stale: bool = False,
    ) -> dict[str, Any] | None:
        """Return a cached public character profile without network access."""
        character_id = int(character_id)
        key = f"character:{character_id}"
        cached = self.cache.get(key)
        if cached is None and allow_stale:
            cached = self.cache.get_stale(key)
        if not isinstance(cached, dict):
            return None
        profile = dict(cached)
        profile.setdefault("character_id", character_id)
        profile.setdefault(
            "zkill_url",
            f"https://zkillboard.com/character/{character_id}/",
        )
        profile.update(self.cache.metadata(key))
        return profile

    def _resolve_names_locked(self, names: list[str]) -> list[ResolvedName]:
        """Resolve one batch while holding the cache-miss coalescing lock."""
        clean_names = [name.strip() for name in names if name and name.strip()]
        cached: dict[str, ResolvedName] = {}
        missing: list[str] = []
        for name in clean_names:
            key = self._name_key(name)
            value = self.cache.get(key)
            if isinstance(value, dict):
                if value.get("status") == "not_found":
                    continue
                cached[name.casefold()] = ResolvedName(
                    name=str(value["name"]),
                    category=str(value["category"]),
                    entity_id=int(value["id"]),
                )
            else:
                missing.append(name)

        if missing:
            resolved = self._resolve_missing(missing)
            resolved_keys = {item.name.casefold() for item in resolved}
            for item in resolved:
                cached[item.name.casefold()] = item
                self.cache.set(
                    self._name_key(item.name),
                    {
                        "name": item.name,
                        "category": item.category,
                        "id": item.entity_id,
                    },
                    ttl_seconds=self.ttl_seconds,
                )
            for name in missing:
                if name.casefold() in resolved_keys:
                    continue
                self.cache.set(
                    self._name_key(name),
                    {
                        "name": name,
                        "status": "not_found",
                    },
                    ttl_seconds=self.negative_ttl_seconds,
                )
            self.cache.save()

        result: list[ResolvedName] = []
        for name in clean_names:
            item = cached.get(name.casefold())
            if item is not None:
                result.append(item)
        return result

    def character_profile(self, character_id: int) -> dict[str, Any]:
        """Return cached public character profile data."""
        key = f"character:{int(character_id)}"
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            profile = dict(cached)
            profile.setdefault(
                "zkill_url",
                f"https://zkillboard.com/character/{int(character_id)}/",
            )
            changed = self._refresh_character_affiliation(profile)
            if self._complete_character_affiliations(profile):
                changed = True
            if changed:
                self.cache.set(key, profile, ttl_seconds=self.profile_ttl_seconds)
                self.cache.save()
                profile.update(self.cache.metadata(key))
                profile["cache_status"] = "refreshed"
                return profile
            profile.update(self.cache.metadata(key))
            return profile

        character = self.client.get_character(int(character_id))
        profile = {
            "character_id": int(character_id),
            "name": str(character.get("name", "")),
            "zkill_url": f"https://zkillboard.com/character/{int(character_id)}/",
            "corporation_id": _optional_int(character.get("corporation_id")),
            "alliance_id": _optional_int(character.get("alliance_id")),
            "security_status": character.get("security_status"),
        }
        self._refresh_character_affiliation(profile)
        self._complete_character_affiliations(profile)
        self.cache.set(key, profile, ttl_seconds=self.profile_ttl_seconds)
        self.cache.save()
        profile.update(self.cache.metadata(key))
        profile["cache_status"] = "refreshed"
        return profile

    def _refresh_character_affiliation(self, profile: dict[str, Any]) -> bool:
        """Refresh current corporation/alliance from the bulk affiliation route."""
        method = getattr(self.client, "get_character_affiliations", None)
        if not callable(method):
            return False
        try:
            character_id = int(profile["character_id"])
        except (KeyError, TypeError, ValueError):
            return False
        key = f"affiliation:character:{character_id}"
        cached = self.cache.get(key)
        row = cached if isinstance(cached, dict) else None
        if row is None:
            try:
                rows = method([character_id])
            except (EsiApiError, OSError, TimeoutError, ValueError, TypeError):
                return False
            if not isinstance(rows, list):
                return False
            row = next(
                (
                    item
                    for item in rows
                    if isinstance(item, dict)
                    and _optional_int(item.get("character_id")) == character_id
                ),
                None,
            )
            if row is None:
                return False
            row = {
                "character_id": character_id,
                "corporation_id": _optional_int(row.get("corporation_id")),
                "alliance_id": _optional_int(row.get("alliance_id")),
                "faction_id": _optional_int(row.get("faction_id")),
            }
            self.cache.set(key, row, ttl_seconds=self.affiliation_ttl_seconds)
            self.cache.save()

        changed = False
        for field in ("corporation_id", "alliance_id", "faction_id"):
            value = _optional_int(row.get(field))
            if profile.get(field) != value:
                profile[field] = value
                changed = True
                if field == "corporation_id":
                    profile.pop("corporation_name", None)
                elif field == "alliance_id":
                    profile.pop("alliance_name", None)
        return changed

    def corporation_profile(self, corporation_id: int) -> dict[str, Any]:
        """Return cached public corporation profile data."""
        corporation_id = int(corporation_id)
        key = f"corporation:{corporation_id}"
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            profile = dict(cached)
            profile.update(self.cache.metadata(key))
            return profile

        corporation = self.client.get_corporation(corporation_id)
        profile = {
            "corporation_id": corporation_id,
            "name": str(corporation.get("name", "")),
            "ticker": corporation.get("ticker"),
            "alliance_id": _optional_int(corporation.get("alliance_id")),
        }
        self.cache.set(key, profile, ttl_seconds=self.profile_ttl_seconds)
        self.cache.save()
        profile.update(self.cache.metadata(key))
        profile["cache_status"] = "refreshed"
        return profile

    def alliance_profile(self, alliance_id: int) -> dict[str, Any]:
        """Return cached public alliance profile data."""
        alliance_id = int(alliance_id)
        key = f"alliance:{alliance_id}"
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            profile = dict(cached)
            profile.update(self.cache.metadata(key))
            return profile

        alliance = self.client.get_alliance(alliance_id)
        profile = {
            "alliance_id": alliance_id,
            "name": str(alliance.get("name", "")),
            "ticker": alliance.get("ticker"),
        }
        self.cache.set(key, profile, ttl_seconds=self.profile_ttl_seconds)
        self.cache.save()
        profile.update(self.cache.metadata(key))
        profile["cache_status"] = "refreshed"
        return profile

    def system_profile(self, system_id: int) -> dict[str, Any]:
        """Return cached public solar-system data."""
        key = f"system:{int(system_id)}"
        cached = self.cache.get(key)
        if isinstance(cached, dict):
            profile = dict(cached)
            profile.update(self.cache.metadata(key))
            return profile

        system = self.client.get_system(int(system_id))
        profile = {
            "system_id": int(system_id),
            "name": str(system.get("name", "")),
            "constellation_id": system.get("constellation_id"),
            "security_status": system.get("security_status"),
        }
        self.cache.set(key, profile, ttl_seconds=self.ttl_seconds)
        self.cache.save()
        profile.update(self.cache.metadata(key))
        profile["cache_status"] = "refreshed"
        return profile

    def enrich_observation(self, observation: Observation) -> Observation:
        """Fill system_id and character_ids on an observation when possible."""
        names_to_resolve = list(observation.names)
        if observation.system_id is None and observation.system_name:
            names_to_resolve.append(observation.system_name)
        if not names_to_resolve:
            return observation

        try:
            resolved = self.resolve_names(names_to_resolve)
        except EsiApiError:
            return observation

        resolved_character_names: set[str] = set()
        character_ids = list(observation.character_ids)
        system_name_matched = observation.system_id is not None
        for item in resolved:
            if item.category == "character":
                if item.entity_id not in character_ids:
                    character_ids.append(item.entity_id)
                resolved_character_names.add(item.name.casefold())
            if (
                item.category == "solar_system"
                and item.name.casefold() == observation.system_name.casefold()
            ):
                observation.system_id = item.entity_id
                system_name_matched = True
        observation.character_ids = character_ids
        observation.metadata = self._resolution_metadata(
            observation,
            resolved_character_names,
            system_name_matched,
        )
        return observation

    def _resolution_metadata(
        self,
        observation: Observation,
        resolved_character_names: set[str],
        system_name_matched: bool,
    ) -> dict[str, Any]:
        metadata = dict(observation.metadata)
        resolution = _resolution_metadata_dict(metadata.get("esi_resolution"))
        resolved_names: list[str] = []
        unresolved_names: list[str] = []
        for name in observation.names:
            if name.casefold() in resolved_character_names:
                resolved_names.append(name)
            else:
                unresolved_names.append(name)

        resolution["attempted"] = True
        resolution["character_name_count"] = len(observation.names)
        resolution["resolved_character_count"] = len(resolved_names)
        resolution["system_name_matched"] = bool(system_name_matched)
        _set_optional_list(resolution, "resolved_character_names", resolved_names)
        _set_optional_list(resolution, "unresolved_character_names", unresolved_names)
        if observation.system_id is not None:
            resolution["resolved_system_id"] = observation.system_id
        else:
            resolution.pop("resolved_system_id", None)

        metadata["esi_resolution"] = resolution
        return metadata

    def _complete_character_affiliations(self, profile: dict[str, Any]) -> bool:
        """Best-effort corporation/alliance name enrichment."""
        changed = False
        corporation_id = _optional_int(profile.get("corporation_id"))
        if corporation_id is not None and not profile.get("corporation_name"):
            try:
                corporation = self.corporation_profile(corporation_id)
            except EsiApiError:
                corporation = {}
            corporation_name = str(corporation.get("name") or "").strip()
            if corporation_name:
                profile["corporation_name"] = corporation_name
                changed = True

        alliance_id = _optional_int(profile.get("alliance_id"))
        if alliance_id is not None and not profile.get("alliance_name"):
            try:
                alliance = self.alliance_profile(alliance_id)
            except EsiApiError:
                alliance = {}
            alliance_name = str(alliance.get("name") or "").strip()
            if alliance_name:
                profile["alliance_name"] = alliance_name
                changed = True
        return changed

    def _resolve_missing(self, names: list[str]) -> list[ResolvedName]:
        payload = self.client.resolve_ids(names)
        if not isinstance(payload, dict):
            return []

        result: list[ResolvedName] = []
        for category, singular in (
            ("characters", "character"),
            ("corporations", "corporation"),
            ("alliances", "alliance"),
            ("systems", "solar_system"),
            ("regions", "region"),
        ):
            for item in payload.get(category, []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    entity_id = int(item["id"])
                    name = str(item["name"])
                except (KeyError, TypeError, ValueError):
                    continue
                result.append(
                    ResolvedName(
                        name=name,
                        category=singular,
                        entity_id=entity_id,
                    )
                )
        return result

    def _name_key(self, name: str) -> str:
        return f"name:{name.strip().casefold()}"


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _resolution_metadata_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _set_optional_list(
    mapping: dict[str, Any],
    key: str,
    values: list[str],
) -> None:
    if values:
        mapping[key] = list(values)
        return
    mapping.pop(key, None)
