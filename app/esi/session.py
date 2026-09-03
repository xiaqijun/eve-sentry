"""Authenticated ESI session helpers built on saved SSO tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Callable

from app.esi.cache import EsiCache
from app.esi.client import EsiClient
from app.esi.sso import EsiSsoError, EsiTokenStore, EveSsoClient, TokenSet

CHARACTER_CONTACT_SCOPE = "esi-characters.read_contacts.v1"
CORPORATION_CONTACT_SCOPE = "esi-corporations.read_contacts.v1"
ALLIANCE_CONTACT_SCOPE = "esi-alliances.read_contacts.v1"
SEARCH_SCOPE = "esi-search.search_structures.v1"
CHARACTER_STANDINGS_SCOPE = "esi-characters.read_standings.v1"


@dataclass(frozen=True)
class ContactStanding:
    """One contact standing entry fetched through authenticated ESI."""

    contact_id: int
    contact_type: str
    standing: float
    label: str = ""
    source: str = "esi_contacts"

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "contact_type": self.contact_type,
            "standing": self.standing,
            "label": self.label,
            "source": self.source,
        }


@dataclass(frozen=True)
class EsiStanding:
    """One row from an authenticated character standings snapshot."""

    from_id: int
    from_type: str
    standing: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_id": self.from_id,
            "from_type": self.from_type,
            "standing": self.standing,
        }


@dataclass(frozen=True)
class EsiSessionSnapshot:
    """Current authenticated ESI data for the logged-in character."""

    tokens: TokenSet
    location: dict[str, Any] = field(default_factory=dict)
    contacts: list[ContactStanding] = field(default_factory=list)
    standings: list[EsiStanding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.tokens.character_id,
            "character_owner_hash": self.tokens.character_owner_hash,
            "scopes": list(self.tokens.scopes),
            "location": dict(self.location),
            "contacts": [contact.to_dict() for contact in self.contacts],
            "standings": [standing.to_dict() for standing in self.standings],
        }


class EsiAuthenticatedSession:
    """Load, refresh, and use saved EVE SSO tokens for ESI calls."""

    def __init__(
        self,
        sso_client: EveSsoClient | Any,
        esi_client: EsiClient | Any | None = None,
        token_store: EsiTokenStore | None = None,
        now: Callable[[], float] | None = None,
        cache: EsiCache | Any | None = None,
        standing_ttl_seconds: float = 600.0,
    ) -> None:
        self.sso_client = sso_client
        self.esi_client = esi_client or EsiClient()
        self.token_store = token_store or EsiTokenStore()
        self._now = now or time
        self.cache = cache
        self.standing_ttl_seconds = min(900.0, max(300.0, float(standing_ttl_seconds)))
        self._standings_memory: dict[int, tuple[float, list[EsiStanding]]] = {}

    def load_tokens(self, refresh_if_needed: bool = True) -> TokenSet:
        """Load saved tokens and refresh them when they are near expiry."""
        tokens = self.token_store.load()
        if tokens is None:
            raise EsiSsoError("no saved ESI token")
        if refresh_if_needed and tokens.is_expired(now=self._now):
            tokens = self.refresh_tokens(tokens)
        return tokens

    def refresh_tokens(self, tokens: TokenSet) -> TokenSet:
        """Refresh a token set and persist the refreshed result."""
        if not tokens.refresh_token:
            raise EsiSsoError("saved ESI token cannot be refreshed")
        refreshed = self.sso_client.refresh(tokens.refresh_token)
        if not refreshed.refresh_token:
            refreshed = TokenSet.from_payload(
                {**refreshed.to_dict(), "refresh_token": tokens.refresh_token},
            )
        self.token_store.save(refreshed)
        return refreshed

    def snapshot(
        self,
        include_location: bool = True,
        include_contacts: bool = True,
        include_standings: bool = True,
    ) -> EsiSessionSnapshot:
        """Fetch current authenticated location, contacts, and reputation standings."""
        tokens = self.load_tokens()
        character_id = tokens.character_id
        if character_id is None:
            raise EsiSsoError("saved ESI token did not include character_id")

        location: dict[str, Any] = {}
        contacts: list[ContactStanding] = []
        standings: list[EsiStanding] = []
        if include_location:
            location = self.esi_client.get_character_location(
                character_id,
                tokens.access_token,
            )
        if include_contacts:
            contacts = contact_standings_from_payload(
                self.esi_client.get_character_contacts(
                    character_id,
                    tokens.access_token,
                )
            )
            profile = self._authenticated_character_profile(tokens)
            corporation_id = _optional_positive_int(profile.get("corporation_id"))
            alliance_id = _optional_positive_int(profile.get("alliance_id"))
            if (
                corporation_id is not None
                and CORPORATION_CONTACT_SCOPE in set(tokens.scopes)
            ):
                contacts.extend(
                    contact_standings_from_payload(
                        self._optional_contacts(
                            "get_corporation_contacts",
                            corporation_id,
                            tokens.access_token,
                        )
                    )
                )
            if alliance_id is not None and ALLIANCE_CONTACT_SCOPE in set(tokens.scopes):
                contacts.extend(
                    contact_standings_from_payload(
                        self._optional_contacts(
                            "get_alliance_contacts",
                            alliance_id,
                            tokens.access_token,
                        )
                    )
                )
            contacts.append(
                ContactStanding(
                    contact_id=character_id,
                    contact_type="character",
                    standing=10.0,
                    label="self",
                    source="esi_self",
                )
            )
            if corporation_id is not None:
                contacts.append(
                    ContactStanding(
                        contact_id=corporation_id,
                        contact_type="corporation",
                        standing=10.0,
                        label="self corporation",
                        source="esi_self",
                    )
                )
            if alliance_id is not None:
                contacts.append(
                    ContactStanding(
                        contact_id=alliance_id,
                        contact_type="alliance",
                        standing=10.0,
                        label="self alliance",
                        source="esi_self",
                    )
                )
        if include_standings:
            standings = self._cached_character_standings(tokens)
        return EsiSessionSnapshot(
            tokens=tokens,
            location=location,
            contacts=contacts,
            standings=standings,
        )

    def standings(self) -> list[EsiStanding]:
        """Return the cached character standings snapshot for the authorized account."""
        return self._cached_character_standings(self.load_tokens())

    def _cached_character_standings(self, tokens: TokenSet) -> list[EsiStanding]:
        character_id = tokens.character_id
        if character_id is None or CHARACTER_STANDINGS_SCOPE not in set(tokens.scopes):
            return []
        now = float(self._now())
        memory = self._standings_memory.get(character_id)
        if memory is not None and now < memory[0]:
            return list(memory[1])

        key = f"standings:character:{character_id}"
        cached = self.cache.get(key) if self.cache is not None else None
        if isinstance(cached, list):
            standings = standings_from_payload(cached)
            self._standings_memory[character_id] = (
                now + self.standing_ttl_seconds,
                standings,
            )
            return list(standings)

        try:
            rows = self.esi_client.get_character_standings(
                character_id,
                tokens.access_token,
            )
            standings = standings_from_payload(rows)
            if self.cache is not None:
                self.cache.set(
                    key,
                    [standing.to_dict() for standing in standings],
                    ttl_seconds=int(self.standing_ttl_seconds),
                )
                save = getattr(self.cache, "save", None)
                if callable(save):
                    save()
            self._standings_memory[character_id] = (
                now + self.standing_ttl_seconds,
                standings,
            )
            return list(standings)
        except Exception:
            stale = self.cache.get_stale(key) if self.cache is not None else None
            if isinstance(stale, list):
                standings = standings_from_payload(stale)
                self._standings_memory[character_id] = (
                    now + min(60.0, self.standing_ttl_seconds),
                    standings,
                )
                return list(standings)
            if memory is not None:
                return list(memory[1])
            return []

    def complete_character_name(self, prefix: str) -> str | None:
        """Return one unique full character name matching a clipped prefix."""
        text = str(prefix or "").strip()
        if len(text) < 8:
            return None

        tokens = self.load_tokens()
        character_id = tokens.character_id
        if character_id is None or SEARCH_SCOPE not in set(tokens.scopes):
            return None
        if not hasattr(self.esi_client, "search_characters") or not hasattr(
            self.esi_client, "resolve_names"
        ):
            return None

        character_ids = self.esi_client.search_characters(
            character_id,
            tokens.access_token,
            text,
        )
        if not character_ids:
            return None
        rows = self.esi_client.resolve_names(character_ids)
        prefix_key = text.casefold()
        matches: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("category") or "").casefold() != "character":
                continue
            name = str(row.get("name") or "").strip()
            name_key = name.casefold()
            if len(name_key) > len(prefix_key) and name_key.startswith(prefix_key):
                matches[name_key] = name
        if len(matches) == 1:
            return next(iter(matches.values()))
        return None

    def _authenticated_character_profile(self, tokens: TokenSet) -> dict[str, Any]:
        scopes = set(tokens.scopes)
        if not (
            CORPORATION_CONTACT_SCOPE in scopes
            or ALLIANCE_CONTACT_SCOPE in scopes
        ):
            return {}
        character_id = tokens.character_id
        if character_id is None or not hasattr(self.esi_client, "get_character"):
            return {}
        try:
            profile = self.esi_client.get_character(character_id)
        except Exception:
            return {}
        return profile if isinstance(profile, dict) else {}

    def _optional_contacts(
        self,
        method_name: str,
        entity_id: int,
        access_token: str,
    ) -> list[dict[str, Any]]:
        if not hasattr(self.esi_client, method_name):
            return []
        try:
            payload = getattr(self.esi_client, method_name)(entity_id, access_token)
        except Exception:
            return []
        return payload if isinstance(payload, list) else []


def contact_standings_from_payload(rows: Any) -> list[ContactStanding]:
    """Normalize ESI contact rows into typed standing entries."""
    if not isinstance(rows, list):
        return []

    standings = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        contact_id = _optional_positive_int(row.get("contact_id"))
        standing = _optional_float(row.get("standing"))
        if contact_id is None or standing is None:
            continue
        standings.append(
            ContactStanding(
                contact_id=contact_id,
                contact_type=str(row.get("contact_type") or "").strip(),
                standing=standing,
                label=str(row.get("label") or row.get("name") or "").strip(),
            )
        )
    return standings


def standings_from_payload(rows: Any) -> list[EsiStanding]:
    """Normalize the complete character standings response from ESI."""
    if not isinstance(rows, list):
        return []
    standings: list[EsiStanding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        from_id = _optional_positive_int(row.get("from_id"))
        from_type = str(row.get("from_type") or "").strip()
        standing = _optional_float(row.get("standing"))
        if from_id is None or not from_type or standing is None:
            continue
        standings.append(
            EsiStanding(
                from_id=from_id,
                from_type=from_type,
                standing=standing,
            )
        )
    return standings


def apply_contact_standing(
    profile: dict[str, Any],
    contacts: list[ContactStanding],
) -> dict[str, Any]:
    """Return a profile copy annotated with the best matching contact standing."""
    result = dict(profile)
    match = matching_contact_standing(profile, contacts)
    if match is None:
        return result
    result.setdefault("contact_standing", match.standing)
    result["standing_source"] = match.source
    result["standing_contact_id"] = match.contact_id
    result["standing_contact_type"] = match.contact_type
    if match.label:
        result["standing_label"] = match.label
    return result


def matching_contact_standing(
    profile: dict[str, Any],
    contacts: list[ContactStanding],
) -> ContactStanding | None:
    """Return the most specific standing matching a character profile."""
    candidates = [
        ("character", _optional_positive_int(profile.get("character_id"))),
        ("corporation", _optional_positive_int(profile.get("corporation_id"))),
        ("alliance", _optional_positive_int(profile.get("alliance_id"))),
    ]
    by_key = {
        (contact.contact_type.casefold(), contact.contact_id): contact
        for contact in contacts
    }
    for contact_type, contact_id in candidates:
        if contact_id is None:
            continue
        match = by_key.get((contact_type, contact_id))
        if match is not None:
            return match
    return None


def _optional_positive_int(value: Any) -> int | None:
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
