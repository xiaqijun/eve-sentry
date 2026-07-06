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


def test_region_preferences_does_not_reuse_region_for_unknown_window(tmp_path):
    prefs = RegionPreferences(str(tmp_path / "region_prefs.json"))
    saved_window = {"hwnd": 1, "title": "EVE - Pilot A", "x": 0, "y": 0, "w": 800, "h": 600}
    unknown_window = {"hwnd": 2, "title": "EVE - Pilot B", "x": 0, "y": 0, "w": 800, "h": 600}

    prefs.save_region(saved_window, {"x": 600, "y": 100, "w": 180, "h": 300})

    assert prefs.resolve_region(unknown_window) is None
