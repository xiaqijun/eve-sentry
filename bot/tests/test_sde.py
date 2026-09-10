import json
import sqlite3
import zipfile
from types import SimpleNamespace

import httpx
import pytest
import respx

from eve_risk.clients.esi import ESIClient
from eve_risk.sde import SDELocalization, _build_from_url, build_sde_index
from eve_risk.ship_roles import ShipRoleClassifier


@pytest.mark.parametrize("state", ["valid", "missing", "corrupt", "incomplete", "old"])
def test_check_only_validates_local_index_without_network(tmp_path, monkeypatch, state):
    from eve_risk import sde

    index_path = tmp_path / "sde.sqlite3"
    if state in {"valid", "incomplete", "old"}:
        archive_path = tmp_path / "sde.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for member in sde.SDE_MEMBERS:
                archive.writestr(member, "")
        build_sde_index(archive_path, index_path, "123")
        with sqlite3.connect(index_path) as connection:
            if state == "incomplete":
                connection.execute("DROP TABLE types")
            elif state == "old":
                connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
    elif state == "corrupt":
        index_path.write_bytes(b"not sqlite")

    monkeypatch.setattr("eve_risk.config.get_settings", lambda: SimpleNamespace(sde_index_path=index_path))

    def forbidden_sync(*args):
        pytest.fail("check-only must not access the network")

    monkeypatch.setattr(sde, "sync_sde", forbidden_sync)
    if state == "valid":
        sde.main(["--check-only"])
    else:
        with pytest.raises((RuntimeError, sqlite3.Error)):
            sde.main(["--check-only"])


def test_sync_cli_rejects_unusable_index_after_sync(tmp_path, monkeypatch):
    from eve_risk import sde

    settings = SimpleNamespace(sde_index_path=tmp_path / "missing.sqlite3", sde_url="unused")
    monkeypatch.setattr("eve_risk.config.get_settings", lambda: settings)
    calls = []
    monkeypatch.setattr(sde, "sync_sde", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="No usable SDE"):
        sde.main([])
    assert calls == [(settings.sde_url, settings.sde_index_path)]


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
