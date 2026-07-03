from app.server.map_config import MapConfigStore


def _write_sde_fixture(root):
    bsd_dir = root / "bsd"
    bsd_dir.mkdir(parents=True)
    (bsd_dir / "mapRegions.yaml").write_text(
        """
- regionID: 10000033
  regionName: The Citadel
""".strip(),
        encoding="utf-8",
    )
    (bsd_dir / "mapConstellations.yaml").write_text(
        """
- constellationID: 20000345
  regionID: 10000033
""".strip(),
        encoding="utf-8",
    )
    (bsd_dir / "mapSolarSystems.yaml").write_text(
        """
- solarSystemID: 30002813
  solarSystemName: Tama
  constellationID: 20000345
  regionID: 10000033
  security: 0.3
  x: -10.0
  z: 50.0
- solarSystemID: 30002819
  solarSystemName: Kedama
  constellationID: 20000345
  regionID: 10000033
  security: 0.2
  x: 90.0
  z: -20.0
""".strip(),
        encoding="utf-8",
    )
    (bsd_dir / "mapSolarSystemJumps.yaml").write_text(
        """
- fromSolarSystemID: 30002813
  toSolarSystemID: 30002819
""".strip(),
        encoding="utf-8",
    )


def _write_universe_sde_fixture(root):
    system_dir = root / "sde" / "universe" / "eve" / "Tenal" / "09-4XW"
    system_dir.mkdir(parents=True)
    (system_dir.parent / "region.yaml").write_text(
        """
regionID: 10000045
""".strip(),
        encoding="utf-8",
    )
    (system_dir / "constellation.yaml").write_text(
        """
constellationID: 20000400
regionID: 10000045
""".strip(),
        encoding="utf-8",
    )
    (system_dir / "1QH-0K").mkdir()
    (system_dir / "ZJ-QOO").mkdir()
    (system_dir / "1QH-0K" / "solarsystem.yaml").write_text(
        """
solarSystemID: 30003617
security: -0.1
center: [-100.0, 0.0, 50.0]
stargates:
  50000001:
    destination: 50000002
""".strip(),
        encoding="utf-8",
    )
    (system_dir / "ZJ-QOO" / "solarsystem.yaml").write_text(
        """
solarSystemID: 30003618
security: -0.2
center: [200.0, 0.0, -80.0]
stargates:
  50000002:
    destination: 50000001
""".strip(),
        encoding="utf-8",
    )


def _write_official_flat_sde_fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "mapRegions.yaml").write_text(
        """
10000045:
  name:
    en: Tenal
  constellationIDs:
    - 20000400
""".strip(),
        encoding="utf-8",
    )
    (root / "mapConstellations.yaml").write_text(
        """
20000400:
  name:
    en: 09-4XW
  regionID: 10000045
  solarSystemIDs:
    - 30003617
    - 30003618
""".strip(),
        encoding="utf-8",
    )
    (root / "mapSolarSystems.yaml").write_text(
        """
30003617:
  constellationID: 20000400
  regionID: 10000045
  security: -0.1
  name:
    en: 1QH-0K
  position:
    x: -100.0
    y: 0.0
    z: 50.0
30003618:
  constellationID: 20000400
  regionID: 10000045
  security: -0.2
  name:
    en: ZJ-QOO
  position:
    x: 200.0
    y: 0.0
    z: -80.0
""".strip(),
        encoding="utf-8",
    )
    (root / "mapStargates.yaml").write_text(
        """
50000001:
  solarSystemID: 30003617
  destination:
    solarSystemID: 30003618
    stargateID: 50000002
50000002:
  solarSystemID: 30003618
  destination:
    solarSystemID: 30003617
    stargateID: 50000001
""".strip(),
        encoding="utf-8",
    )


def _write_official_flat_sde_fixture_with_position2d(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "mapRegions.yaml").write_text(
        """
10000045:
  name:
    en: Tenal
  constellationIDs:
    - 20000400
""".strip(),
        encoding="utf-8",
    )
    (root / "mapConstellations.yaml").write_text(
        """
20000400:
  name:
    en: 09-4XW
  regionID: 10000045
  solarSystemIDs:
    - 30003617
    - 30003618
    - 30003619
""".strip(),
        encoding="utf-8",
    )
    (root / "mapSolarSystems.yaml").write_text(
        """
30003617:
  constellationID: 20000400
  regionID: 10000045
  security: -0.1
  name:
    en: 1QH-0K
  position:
    x: 0.0
    y: 0.0
    z: 0.0
  position2D:
    x: 0.0
    y: 100.0
30003618:
  constellationID: 20000400
  regionID: 10000045
  security: -0.2
  name:
    en: ZJ-QOO
  position:
    x: 1000.0
    y: 0.0
    z: 4000.0
  position2D:
    x: 100.0
    y: 100.0
30003619:
  constellationID: 20000400
  regionID: 10000045
  security: -0.3
  name:
    en: Y-1W01
  position:
    x: 2000.0
    y: 0.0
    z: 8000.0
  position2D:
    x: 200.0
    y: 0.0
""".strip(),
        encoding="utf-8",
    )
    (root / "mapStargates.yaml").write_text(
        """
50000001:
  solarSystemID: 30003617
  destination:
    solarSystemID: 30003618
    stargateID: 50000002
50000002:
  solarSystemID: 30003618
  destination:
    solarSystemID: 30003617
    stargateID: 50000001
50000003:
  solarSystemID: 30003618
  destination:
    solarSystemID: 30003619
    stargateID: 50000004
50000004:
  solarSystemID: 30003619
  destination:
    solarSystemID: 30003618
    stargateID: 50000003
""".strip(),
        encoding="utf-8",
    )


def test_map_config_defaults_to_builtin(tmp_path):
    config = MapConfigStore(tmp_path / "intel_map.json")

    systems, links = config.build_map()

    assert config.source == "builtin"
    assert "Tama" in systems
    assert links


def test_map_config_can_refresh_from_sde(tmp_path):
    sde_root = tmp_path / "sde"
    _write_sde_fixture(sde_root)
    config = MapConfigStore(tmp_path / "intel_map.json")
    config.update(
        {
            "source": "sde",
            "sde_path": str(sde_root),
            "region_ids": [10000033],
        }
    )

    refreshed = config.refresh_from_source()
    systems, links = config.build_map(refresh_if_needed=False)

    assert refreshed["source"] == "sde"
    assert refreshed["layout_mode"] == "sde"
    assert refreshed["last_refreshed_at"]
    assert refreshed["last_refresh_error"] == ""
    assert config.sde_path == str(sde_root)
    assert set(systems) == {"Tama", "Kedama"}
    assert systems["Tama"].system_id == 30002813
    assert systems["Tama"].region == "The Citadel"
    assert links == [("Tama", "Kedama")]


def test_map_config_can_refresh_from_universe_layout(tmp_path):
    sde_root = tmp_path / "sde-universe"
    _write_universe_sde_fixture(sde_root)
    config = MapConfigStore(tmp_path / "intel_map.json")
    config.update(
        {
            "source": "sde",
            "sde_path": str(sde_root),
            "region_ids": [10000045],
        }
    )

    refreshed = config.refresh_from_source()
    systems, links = config.build_map(refresh_if_needed=False)

    assert refreshed["source"] == "sde"
    assert set(systems) == {"1QH-0K", "ZJ-QOO"}
    assert systems["1QH-0K"].system_id == 30003617
    assert systems["1QH-0K"].region == "Tenal"
    assert links == [("1QH-0K", "ZJ-QOO")]


def test_map_config_can_refresh_from_official_flat_tables(tmp_path):
    sde_root = tmp_path / "sde-flat"
    _write_official_flat_sde_fixture(sde_root)
    config = MapConfigStore(tmp_path / "intel_map.json")
    config.update(
        {
            "source": "sde",
            "sde_path": str(sde_root),
            "region_ids": [10000045],
        }
    )

    refreshed = config.refresh_from_source()
    systems, links = config.build_map(refresh_if_needed=False)

    assert refreshed["source"] == "sde"
    assert refreshed["layout_mode"] == "sde"
    assert set(systems) == {"1QH-0K", "ZJ-QOO"}
    assert systems["1QH-0K"].system_id == 30003617
    assert systems["1QH-0K"].region == "Tenal"
    assert links == [("1QH-0K", "ZJ-QOO")]


def test_map_config_prefers_position2d_for_official_flat_tables(tmp_path):
    sde_root = tmp_path / "sde-flat-pos2d"
    _write_official_flat_sde_fixture_with_position2d(sde_root)
    config = MapConfigStore(tmp_path / "intel_map.json")
    config.update(
        {
            "source": "sde",
            "sde_path": str(sde_root),
            "region_ids": [10000045],
        }
    )

    config.refresh_from_source()
    systems, _links = config.build_map(refresh_if_needed=False)

    assert systems["1QH-0K"].y == systems["ZJ-QOO"].y
    assert systems["Y-1W01"].y > systems["1QH-0K"].y
