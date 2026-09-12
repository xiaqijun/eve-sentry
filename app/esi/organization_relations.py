"""Immutable organization-only standings and ordered, constant-time classification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.esi.contact_http import validate_rows
from app.esi.personnel_policy import positive_id, timestamp

RULE_VERSION = "organization.v1"
STANDING_SOURCE = "esi_organization"
PENDING_SOURCE = "esi_organization_pending"


@dataclass(frozen=True)
class RelationSource:
    """One complete authorized source; failure metadata never renews successful_at."""

    kind: str
    entity_id: int
    revision: int = 0
    successful_at: float | None = None
    expires_at: float = 0.0
    retry_at: float = 0.0
    failures: int = 0
    authorized: bool = True
    entries: Mapping = field(default_factory=lambda: MappingProxyType({}))

    def usable(self, now: float) -> bool:
        return (self.authorized and self.successful_at is not None
                and self.successful_at <= now < self.successful_at + 3600)


def organization_entries(rows: list[dict]) -> Mapping:
    """Validate the full response before filtering individuals/factions out of active data."""
    validate_rows(rows)
    return MappingProxyType({(row["contact_type"], row["contact_id"]): float(row["standing"])
                             for row in rows if row["contact_type"] in {"corporation", "alliance"}})


@dataclass(frozen=True)
class RelationView:
    """A pinned classification generation; no database, token reads or dict rebuilding."""

    context: str
    corporation_id: int | None
    alliance_id: int | None
    own_usable: bool
    sources: tuple[RelationSource, ...]
    usable_sources: tuple[bool, ...]
    rule_version: str = RULE_VERSION

    def __bool__(self) -> bool:
        # Even unknown views must run annotation to strip legacy personal standings.
        return True

    def annotate(self, profile: dict) -> dict:
        result = dict(profile)
        for key in ("contact_standing", "standing", "standing_source", "standing_contact_id",
                    "standing_contact_type", "standing_label"):
            result.pop(key, None)
        result["standing_source"] = PENDING_SOURCE
        if not self.own_usable or profile.get("affiliation_trusted") is False:
            return result
        corporation = profile.get("corporation_id")
        alliance = profile.get("alliance_id")
        if type(corporation) is not int or corporation <= 0:
            return result
        if alliance is not None and (type(alliance) is not int or alliance <= 0):
            return result
        if corporation == self.corporation_id:
            match = "corporation", corporation, 10.0
        elif alliance is not None and alliance == self.alliance_id:
            match = "alliance", alliance, 10.0
        else:
            match = None
            for kind, entity_id in (("corporation", corporation), ("alliance", alliance)):
                if entity_id is None:
                    continue
                for source, usable in zip(self.sources, self.usable_sources, strict=True):
                    # Unknown higher-priority sources cannot be silently skipped.
                    if not usable:
                        return result
                    key = kind, entity_id
                    if key in source.entries:
                        match = kind, entity_id, source.entries[key]
                        break
                if match is not None:
                    break
            if match is None:
                match = "neutral", corporation, 0.0
        result.update(standing_source=STANDING_SOURCE, standing_contact_type=match[0],
                      standing_contact_id=match[1], contact_standing=match[2])
        return result


def validate_source(source: RelationSource) -> None:
    if source.kind not in {"corporation", "alliance"}:
        raise ValueError("invalid relation source kind")
    if len(source.entries) > 20000:
        raise ValueError("relation snapshot exceeds row limit")
    positive_id(source.entity_id)
    if type(source.revision) is not int or source.revision < 0:
        raise ValueError("invalid relation revision")
    if type(source.failures) is not int or source.failures < 0 or type(source.authorized) is not bool:
        raise ValueError("invalid relation status")
    for value in (source.expires_at, source.retry_at):
        timestamp(value)
    if source.successful_at is not None:
        timestamp(source.successful_at)
    rows = [{"contact_type": kind, "contact_id": cid, "standing": standing}
            for (kind, cid), standing in source.entries.items()]
    if any(row["contact_type"] not in {"corporation", "alliance"} for row in rows):
        raise ValueError("personal relations are not active organization data")
    validate_rows(rows)
