from __future__ import annotations

import json
from pathlib import Path

from eve_risk.domain import ShipRole

GROUP_ROLE_RULES: tuple[tuple[tuple[str, ...], ShipRole], ...] = (
    (("force auxiliary", "carrier", "supercarrier", "dreadnought", "titan"), ShipRole.CAPITAL),
    (("logistics",), ShipRole.LOGISTICS),
    (("interdictor", "heavy interdictor"), ShipRole.INTERDICTION),
    (
        (
            "industrial command",
            "industrial",
            "freighter",
            "mining",
            "exhumer",
            "barge",
            "hauler",
            "transport ship",
            "capital industrial",
        ),
        ShipRole.INDUSTRIAL,
    ),
    (("command ship", "command destroyer"), ShipRole.COMMAND),
    (("electronic attack",), ShipRole.EWAR),
    (("recon ship", "combat recon", "force recon"), ShipRole.EWAR),
    (("interceptor",), ShipRole.TACKLE),
    (("covert ops", "black ops"), ShipRole.SCOUT),
)

COMBAT_KEYWORDS = (
    "frigate",
    "destroyer",
    "cruiser",
    "battlecruiser",
    "battleship",
    "assault ship",
    "strategic cruiser",
    "marauder",
    "gunship",
)


class ShipRoleClassifier:
    def __init__(self, overrides_path: Path | None = None) -> None:
        self.overrides: dict[int, ShipRole] = {}
        if overrides_path and overrides_path.exists():
            raw = json.loads(overrides_path.read_text(encoding="utf-8"))
            self.overrides = {int(type_id): ShipRole(role) for type_id, role in raw.items()}

    def classify(self, type_id: int, group_name: str, category_id: int | None = None) -> ShipRole:
        if type_id in self.overrides:
            return self.overrides[type_id]

        normalized = group_name.casefold()
        for keywords, role in GROUP_ROLE_RULES:
            if any(keyword in normalized for keyword in keywords):
                return role
        if category_id == 6 or any(keyword in normalized for keyword in COMBAT_KEYWORDS):
            return ShipRole.DPS
        return ShipRole.OTHER
