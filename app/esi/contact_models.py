"""Public contact-standing models used by personnel classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContactStanding:
    """One normalized organization or character standing entry."""

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


def contact_standings_from_payload(rows: Any) -> list[ContactStanding]:
    """Normalize a public contact payload into typed standing entries."""
    if not isinstance(rows, list):
        return []

    standings: list[ContactStanding] = []
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


def apply_contact_standing(
    profile: dict[str, Any],
    contacts: list[ContactStanding],
) -> dict[str, Any]:
    """Return a profile copy annotated with the best matching standing."""
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
