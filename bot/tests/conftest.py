from datetime import UTC, datetime

import pytest

from eve_risk.domain import CharacterIdentity, ShipRole, ShipTypeInfo


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def identities() -> list[CharacterIdentity]:
    return [
        CharacterIdentity(
            character_id=1,
            name="Alice Example",
            corporation_id=101,
            corporation_name="Alpha Corp",
            alliance_id=201,
            alliance_name="Alliance One",
        ),
        CharacterIdentity(
            character_id=2,
            name="Bob Example",
            corporation_id=102,
            corporation_name="Beta Corp",
            alliance_id=201,
            alliance_name="Alliance One",
        ),
    ]


@pytest.fixture
def ship_types() -> dict[int, ShipTypeInfo]:
    return {
        1001: ShipTypeInfo(
            type_id=1001,
            name="Damage Cruiser",
            group_id=10,
            group_name="Heavy Assault Cruiser",
            category_id=6,
            role=ShipRole.DPS,
        ),
        1002: ShipTypeInfo(
            type_id=1002,
            name="Logistics Cruiser",
            group_id=11,
            group_name="Logistics",
            category_id=6,
            role=ShipRole.LOGISTICS,
        ),
    }
