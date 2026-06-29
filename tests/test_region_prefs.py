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
