from app.ui.region_selector import (
    map_rect_between_geometries,
    region_selector_hint_lines,
    region_selector_overlay_color,
)


def test_region_selector_hint_lines_include_window_title_and_actions():
    lines = region_selector_hint_lines("EVE - Hajimi6")

    assert lines == [
        "正在选择窗口: EVE - Hajimi6",
        "请拖拽框选该窗口内的成员列表区域",
        "松开鼠标确认, 按 Esc 取消",
    ]


def test_region_selector_overlay_color_is_translucent():
    color = region_selector_overlay_color()

    assert 0 < color.alpha() < 160


def test_map_rect_between_physical_and_logical_monitor_geometries():
    physical_monitor = {"x": 1920, "y": 0, "w": 3840, "h": 2160}
    logical_monitor = {"x": 1920, "y": 0, "w": 2560, "h": 1440}
    physical_window = {"x": 2304, "y": 216, "w": 1920, "h": 1080}

    assert map_rect_between_geometries(
        physical_window,
        physical_monitor,
        logical_monitor,
    ) == {"x": 2176, "y": 144, "w": 1280, "h": 720}


def test_map_rect_returns_physical_selection_from_scaled_overlay():
    logical_selection = {"x": 960, "y": 72, "w": 320, "h": 576}
    logical_window = {"x": 0, "y": 0, "w": 1280, "h": 720}
    physical_window = {"x": 2304, "y": 216, "w": 1920, "h": 1080}

    assert map_rect_between_geometries(
        logical_selection,
        logical_window,
        physical_window,
    ) == {"x": 3744, "y": 324, "w": 480, "h": 864}


def test_map_rect_handles_negative_origin_secondary_monitor():
    physical_monitor = {"x": -2560, "y": -200, "w": 2560, "h": 1440}
    logical_monitor = {"x": -1707, "y": -133, "w": 1707, "h": 960}
    physical_window = {"x": -2400, "y": -80, "w": 1280, "h": 720}

    assert map_rect_between_geometries(
        physical_window,
        physical_monitor,
        logical_monitor,
    ) == {"x": -1600, "y": -53, "w": 854, "h": 480}
