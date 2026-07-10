from app.models.region_prefs import RegionPreferences


def test_region_preferences_round_trip(tmp_path):
    prefs = RegionPreferences(str(tmp_path / "region_prefs.json"))
    window = {"x": 100, "y": 50, "w": 800, "h": 600}
    region = {"x": 300, "y": 200, "w": 160, "h": 180}

    prefs.save_region(window, region)
    restored = prefs.resolve_region(window)

    assert restored == region


def test_region_preferences_scale_to_new_window_size(tmp_path):
    prefs = RegionPreferences(str(tmp_path / "region_prefs.json"))
    window = {"x": 0, "y": 0, "w": 1000, "h": 800}
    region = {"x": 100, "y": 160, "w": 300, "h": 200}
    prefs.save_region(window, region)

    moved_window = {"x": 50, "y": 20, "w": 2000, "h": 1600}
    restored = prefs.resolve_region(moved_window)

    assert restored == {"x": 250, "y": 340, "w": 600, "h": 400}


def test_region_preferences_keeps_regions_per_window(tmp_path):
    prefs = RegionPreferences(str(tmp_path / "region_prefs.json"))
    first_window = {"hwnd": 1, "title": "EVE - Pilot A", "x": 0, "y": 0, "w": 800, "h": 600}
    second_window = {"hwnd": 2, "title": "EVE - Pilot B", "x": 20, "y": 30, "w": 1000, "h": 800}
    first_region = {"x": 600, "y": 100, "w": 180, "h": 300}
    second_region = {"x": 760, "y": 190, "w": 220, "h": 420}

    prefs.save_region(first_window, first_region)
    prefs.save_region(second_window, second_region)

    assert prefs.resolve_region(first_window) == first_region
    assert prefs.resolve_region(second_window) == second_region


def test_region_preferences_keeps_regions_per_same_title_window(tmp_path):
    prefs = RegionPreferences(str(tmp_path / "region_prefs.json"))
    first_window = {"hwnd": 1, "title": "EVE - Pilot", "x": 0, "y": 0, "w": 800, "h": 600}
    second_window = {"hwnd": 2, "title": "EVE - Pilot", "x": 20, "y": 30, "w": 1000, "h": 800}
    first_region = {"x": 600, "y": 100, "w": 180, "h": 300}
    second_region = {"x": 760, "y": 190, "w": 220, "h": 420}

    prefs.save_region(first_window, first_region)
    prefs.save_region(second_window, second_region)

    assert prefs.resolve_region(first_window) == first_region
    assert prefs.resolve_region(second_window) == second_region


def test_region_preferences_reads_legacy_title_key_for_window(tmp_path):
    prefs_path = tmp_path / "region_prefs.json"
    prefs_path.write_text(
        """
{
  "member_list_regions": {
    "eve - pilot": {
      "x_ratio": 0.75,
      "y_ratio": 0.1,
      "w_ratio": 0.2,
      "h_ratio": 0.5
    }
  }
}
""".strip(),
        encoding="utf-8",
    )
    prefs = RegionPreferences(str(prefs_path))
    window = {"hwnd": 99, "title": "EVE - Pilot", "x": 10, "y": 20, "w": 1000, "h": 800}

    assert prefs.resolve_region(window) == {"x": 760, "y": 100, "w": 200, "h": 400}


def test_region_preferences_reuses_same_title_when_hwnd_changes(tmp_path):
    prefs_path = tmp_path / "region_prefs.json"
    prefs_path.write_text(
        """
{
  "member_list_region": {
    "x_ratio": 0.1,
    "y_ratio": 0.1,
    "w_ratio": 0.1,
    "h_ratio": 0.1
  },
  "member_list_regions": {
    "hwnd:135158:eve - hajimi6": {
      "x_ratio": 0.17135416666666667,
      "y_ratio": 0.19028741328047571,
      "w_ratio": 0.09322916666666667,
      "h_ratio": 0.755203171456888
    }
  }
}
""".strip(),
        encoding="utf-8",
    )
    prefs = RegionPreferences(str(prefs_path))
    window = {
        "hwnd": 2296266,
        "title": "EVE - Hajimi6",
        "x": 7,
        "y": 31,
        "w": 1920,
        "h": 1009,
    }

    assert prefs.resolve_region(window) == {"x": 336, "y": 223, "w": 179, "h": 762}


def test_region_preferences_does_not_reuse_region_for_unknown_window(tmp_path):
    prefs = RegionPreferences(str(tmp_path / "region_prefs.json"))
    saved_window = {"hwnd": 1, "title": "EVE - Pilot A", "x": 0, "y": 0, "w": 800, "h": 600}
    unknown_window = {"hwnd": 2, "title": "EVE - Pilot B", "x": 0, "y": 0, "w": 800, "h": 600}

    prefs.save_region(saved_window, {"x": 600, "y": 100, "w": 180, "h": 300})

    assert prefs.resolve_region(unknown_window) is None
