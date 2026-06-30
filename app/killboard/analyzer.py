"""Analyze killboard rows into compact threat-behavior profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class KillActivity:
    """Recent kill/loss behavior for one character."""

    character_id: int
    window: str
    kills: int = 0
    losses: int = 0
    systems: list[int] = field(default_factory=list)
    ship_type_ids: list[int] = field(default_factory=list)
    latest_kill_at: str = ""
    source: str = "zkillboard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "window": self.window,
            "kills": self.kills,
            "losses": self.losses,
            "systems": list(self.systems),
            "ship_type_ids": list(self.ship_type_ids),
            "latest_kill_at": self.latest_kill_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class SystemKillActivity:
    """Recent killmail activity in one solar system."""

    system_id: int
    window: str
    kills: int = 0
    character_ids: list[int] = field(default_factory=list)
    ship_type_ids: list[int] = field(default_factory=list)
    latest_kill_at: str = ""
    source: str = "zkillboard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "window": self.window,
            "kills": self.kills,
            "character_ids": list(self.character_ids),
            "ship_type_ids": list(self.ship_type_ids),
            "latest_kill_at": self.latest_kill_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class GroupKillActivity:
    """Recent killmail activity involving one corporation or alliance."""

    entity_type: str
    entity_id: int
    window: str
    kills: int = 0
    losses: int = 0
    systems: list[int] = field(default_factory=list)
    character_ids: list[int] = field(default_factory=list)
    ship_type_ids: list[int] = field(default_factory=list)
    latest_kill_at: str = ""
    source: str = "zkillboard"

    def to_dict(self) -> dict[str, Any]:
        id_key = f"{self.entity_type}_id"
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            id_key: self.entity_id,
            "window": self.window,
            "kills": self.kills,
            "losses": self.losses,
            "systems": list(self.systems),
            "character_ids": list(self.character_ids),
            "ship_type_ids": list(self.ship_type_ids),
            "latest_kill_at": self.latest_kill_at,
            "source": self.source,
        }


def analyze_character_activity(
    character_id: int,
    killmails: list[dict[str, Any]],
    window: str = "recent",
) -> KillActivity:
    """Summarize recent killmails for a character."""
    kills = 0
    losses = 0
    system_ids: set[int] = set()
    ship_type_ids: set[int] = set()
    latest_kill_at = ""

    for killmail in killmails:
        if not isinstance(killmail, dict):
            continue
        if _is_loss(character_id, killmail):
            losses += 1
        if _is_kill(character_id, killmail):
            kills += 1

        system_id = _optional_int(killmail.get("solar_system_id"))
        if system_id is not None:
            system_ids.add(system_id)

        victim = killmail.get("victim")
        if isinstance(victim, dict):
            ship_type_id = _optional_int(victim.get("ship_type_id"))
            if ship_type_id is not None:
                ship_type_ids.add(ship_type_id)

        killmail_time = str(killmail.get("killmail_time") or "")
        if killmail_time > latest_kill_at:
            latest_kill_at = killmail_time

    return KillActivity(
        character_id=int(character_id),
        window=window,
        kills=kills,
        losses=losses,
        systems=sorted(system_ids),
        ship_type_ids=sorted(ship_type_ids),
        latest_kill_at=latest_kill_at,
    )


def analyze_group_activity(
    entity_id: int,
    killmails: list[dict[str, Any]],
    entity_type: str,
    window: str = "recent",
) -> GroupKillActivity:
    """Summarize recent killmails involving a corporation or alliance."""
    normalized_type = entity_type.strip().casefold()
    if normalized_type not in {"corporation", "alliance"}:
        raise ValueError("entity_type must be corporation or alliance")

    id_key = f"{normalized_type}_id"
    kills = 0
    losses = 0
    system_ids: set[int] = set()
    character_ids: set[int] = set()
    ship_type_ids: set[int] = set()
    latest_kill_at = ""

    for killmail in killmails:
        if not isinstance(killmail, dict):
            continue

        loss = _victim_matches(entity_id, id_key, killmail)
        kill = _attacker_matches(entity_id, id_key, killmail)
        if not loss and not kill:
            continue
        losses += 1 if loss else 0
        kills += 1 if kill else 0

        system_id = _optional_int(killmail.get("solar_system_id"))
        if system_id is not None:
            system_ids.add(system_id)

        _collect_participants(killmail, character_ids, ship_type_ids)
        killmail_time = str(killmail.get("killmail_time") or "")
        if killmail_time > latest_kill_at:
            latest_kill_at = killmail_time

    return GroupKillActivity(
        entity_type=normalized_type,
        entity_id=int(entity_id),
        window=window,
        kills=kills,
        losses=losses,
        systems=sorted(system_ids),
        character_ids=sorted(character_ids),
        ship_type_ids=sorted(ship_type_ids),
        latest_kill_at=latest_kill_at,
    )


def analyze_system_activity(
    system_id: int,
    killmails: list[dict[str, Any]],
    window: str = "recent",
) -> SystemKillActivity:
    """Summarize recent killmails in a solar system."""
    kills = 0
    character_ids: set[int] = set()
    ship_type_ids: set[int] = set()
    latest_kill_at = ""

    for killmail in killmails:
        if not isinstance(killmail, dict):
            continue
        killmail_system_id = _optional_int(killmail.get("solar_system_id"))
        if killmail_system_id is not None and killmail_system_id != int(system_id):
            continue

        kills += 1
        victim = killmail.get("victim")
        if isinstance(victim, dict):
            victim_id = _optional_int(victim.get("character_id"))
            ship_type_id = _optional_int(victim.get("ship_type_id"))
            if victim_id is not None:
                character_ids.add(victim_id)
            if ship_type_id is not None:
                ship_type_ids.add(ship_type_id)

        attackers = killmail.get("attackers")
        if isinstance(attackers, list):
            for attacker in attackers:
                if not isinstance(attacker, dict):
                    continue
                attacker_id = _optional_int(attacker.get("character_id"))
                ship_type_id = _optional_int(attacker.get("ship_type_id"))
                if attacker_id is not None:
                    character_ids.add(attacker_id)
                if ship_type_id is not None:
                    ship_type_ids.add(ship_type_id)

        killmail_time = str(killmail.get("killmail_time") or "")
        if killmail_time > latest_kill_at:
            latest_kill_at = killmail_time

    return SystemKillActivity(
        system_id=int(system_id),
        window=window,
        kills=kills,
        character_ids=sorted(character_ids),
        ship_type_ids=sorted(ship_type_ids),
        latest_kill_at=latest_kill_at,
    )


def activity_score_bonus(activity: KillActivity) -> int:
    """Return the initial score bonus contributed by kill activity."""
    if activity.kills >= 5:
        return 20
    if activity.kills >= 1:
        return 10
    return 0


def _is_loss(character_id: int, killmail: dict[str, Any]) -> bool:
    victim = killmail.get("victim")
    if not isinstance(victim, dict):
        return False
    return _optional_int(victim.get("character_id")) == int(character_id)


def _is_kill(character_id: int, killmail: dict[str, Any]) -> bool:
    attackers = killmail.get("attackers")
    if not isinstance(attackers, list):
        return False
    for attacker in attackers:
        if not isinstance(attacker, dict):
            continue
        if _optional_int(attacker.get("character_id")) == int(character_id):
            return True
    return False


def _victim_matches(entity_id: int, id_key: str, killmail: dict[str, Any]) -> bool:
    victim = killmail.get("victim")
    if not isinstance(victim, dict):
        return False
    return _optional_int(victim.get(id_key)) == int(entity_id)


def _attacker_matches(entity_id: int, id_key: str, killmail: dict[str, Any]) -> bool:
    attackers = killmail.get("attackers")
    if not isinstance(attackers, list):
        return False
    for attacker in attackers:
        if not isinstance(attacker, dict):
            continue
        if _optional_int(attacker.get(id_key)) == int(entity_id):
            return True
    return False


def _collect_participants(
    killmail: dict[str, Any],
    character_ids: set[int],
    ship_type_ids: set[int],
) -> None:
    victim = killmail.get("victim")
    if isinstance(victim, dict):
        _add_optional_int(character_ids, victim.get("character_id"))
        _add_optional_int(ship_type_ids, victim.get("ship_type_id"))

    attackers = killmail.get("attackers")
    if not isinstance(attackers, list):
        return
    for attacker in attackers:
        if not isinstance(attacker, dict):
            continue
        _add_optional_int(character_ids, attacker.get("character_id"))
        _add_optional_int(ship_type_ids, attacker.get("ship_type_id"))


def _add_optional_int(values: set[int], value: Any) -> None:
    number = _optional_int(value)
    if number is not None:
        values.add(number)


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
