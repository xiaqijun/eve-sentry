from app.ui.region_selector import (
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
