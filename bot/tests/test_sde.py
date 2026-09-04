import json
import zipfile

import httpx
import pytest
import respx

from eve_risk.clients.esi import ESIClient
from eve_risk.sde import SDELocalization, _build_from_url, build_sde_index
from eve_risk.ship_roles import ShipRoleClassifier


def _line(item: dict[str, object]) -> str:
    return json.dumps(item, ensure_ascii=False) + "\n"


def test_extracts_build_number_from_official_redirect_url() -> None:
    url = (
        "https://developers.eveonline.com/static-data/tranquility/"
        "eve-online-static-data-3433564-jsonl.zip"
    )
    assert _build_from_url(url) == "3433564"


def test_builds_and_reads_official_sde_chinese_index(tmp_path) -> None:
    archive_path = tmp_path / "sde.zip"
    index_path = tmp_path / "sde.sqlite3"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "groups.jsonl",
            _line(
                {
                    "_key": 324,
                    "categoryID": 6,
                    "name": {"en": "Assault Frigate", "zh": "突击护卫舰"},
                }
            ),
        )
        archive.writestr(
            "types.jsonl",
            _line(
                {
                    "_key": 11393,
                    "groupID": 324,
                    "name": {"en": "Retribution", "zh": "惩罚者级海军型"},
                }
            ),
        )
        archive.writestr(
            "mapRegions.jsonl",
            _line(
                {
                    "_key": 10000002,
                    "name": {"en": "The Forge", "zh": "伏尔戈"},
                }
            ),
        )
        archive.writestr(
            "mapSolarSystems.jsonl",
            _line(
                {
                    "_key": 30000142,
                    "regionID": 10000002,
                    "name": {"en": "Jita", "zh": "吉他"},
                }
            ),
        )

    build_sde_index(archive_path, index_path, "123456")
    sde = SDELocalization(index_path)

    assert sde.available is True
    assert sde.build_number == "123456"
    assert sde.type_info(11393) == (
        "惩罚者级海军型",
        "Retribution",
        324,
        "突击护卫舰",
        "Assault Frigate",
        6,
    )
    assert sde.solar_system_name(30000142) == "吉他"
    assert sde.solar_system_info(30000142).region_name == "伏尔戈"
    sde.close()


@pytest.mark.asyncio
async def test_esi_uses_sde_chinese_name_without_type_requests(tmp_path) -> None:
    archive_path = tmp_path / "sde.zip"
    index_path = tmp_path / "sde.sqlite3"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "groups.jsonl",
            _line(
                {
                    "_key": 324,
                    "categoryID": 6,
                    "name": {"en": "Assault Frigate", "zh": "突击护卫舰"},
                }
            ),
        )
        archive.writestr(
            "types.jsonl",
            _line(
                {
                    "_key": 11393,
                    "groupID": 324,
                    "name": {"en": "Retribution", "zh": "惩罚者级海军型"},
                }
            ),
        )
        archive.writestr(
            "mapRegions.jsonl",
            _line({"_key": 10000002, "name": {"en": "The Forge", "zh": "伏尔戈"}}),
        )
        archive.writestr("mapSolarSystems.jsonl", "")
    build_sde_index(archive_path, index_path, "123456")
    sde = SDELocalization(index_path)

    async with httpx.AsyncClient() as http:
        client = ESIClient(
            http,
            "https://esi.evetech.net/latest",
            ShipRoleClassifier(),
            sde=sde,
        )
        with respx.mock(assert_all_called=True):
            ships = await client.fetch_ship_types([11393])

    assert ships[11393].name == "惩罚者级海军型"
    assert ships[11393].group_name == "突击护卫舰"
    assert ships[11393].role.value == "输出舰"
    sde.close()
