import numpy as np
from PIL import Image, ImageDraw

from app.engine.hostile_icons import (
    extract_hostile_name_row_images,
    extract_hostile_name_rows,
    find_hostile_icons,
)


def member_list_fixture() -> Image.Image:
    image = Image.new("RGB", (180, 100), color=(12, 13, 13))
    draw = ImageDraw.Draw(image)

    draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    draw.rectangle((9, 25, 13, 25), fill=(255, 255, 255))
    draw.rectangle((6, 35, 16, 45), fill=(18, 130, 45))
    draw.rectangle((9, 38, 13, 42), fill=(255, 255, 255))
    draw.rectangle((6, 50, 16, 60), fill=(149, 8, 9))
    draw.rectangle((9, 53, 13, 54), fill=(255, 255, 255))
    draw.rectangle((9, 57, 13, 58), fill=(255, 255, 255))

    draw.rectangle((19, 22, 80, 28), fill=(220, 220, 220))
    draw.rectangle((19, 37, 80, 43), fill=(220, 220, 220))
    draw.rectangle((19, 52, 80, 58), fill=(220, 220, 220))
    return image


def test_find_hostile_icons_accepts_both_red_icon_variants():
    icons = find_hostile_icons(member_list_fixture())

    assert [(icon.left, icon.top, icon.right, icon.bottom) for icon in icons] == [
        (6, 20, 17, 31),
        (6, 50, 17, 61),
    ]


def test_extract_hostile_name_rows_excludes_green_rows_and_icons():
    rows = extract_hostile_name_rows(member_list_fixture())

    assert rows is not None
    assert rows.size == (161, 48)
    pixels = np.asarray(rows, dtype=np.int16).reshape(-1, 3)
    assert np.any(np.all(pixels == (220, 220, 220), axis=1))
    assert not np.any(pixels[:, 1] > pixels[:, 0] * 2)
    assert not np.any((pixels[:, 0] >= 100) & ((pixels[:, 0] - pixels[:, 1]) >= 60))


def test_extract_hostile_name_row_images_keeps_each_hostile_on_its_own_row():
    rows = extract_hostile_name_row_images(member_list_fixture())

    assert len(rows) == 2
    assert [row.size for row in rows] == [(161, 16), (161, 16)]


def test_hostile_name_rows_do_not_overlap_adjacent_icon_lines():
    image = Image.new("RGB", (180, 80), color=(12, 13, 13))
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 20, 16, 30), fill=(146, 3, 3))
    draw.rectangle((6, 32, 16, 42), fill=(149, 8, 9))
    draw.rectangle((20, 20, 90, 25), fill=(220, 220, 220))
    draw.rectangle((20, 37, 90, 42), fill=(90, 90, 90))

    rows = extract_hostile_name_row_images(image)

    assert len(rows) == 2
    first_pixels = np.asarray(rows[0])
    second_pixels = np.asarray(rows[1])
    assert not np.any(np.all(first_pixels == (90, 90, 90), axis=2))
    assert not np.any(np.all(second_pixels == (220, 220, 220), axis=2))


def test_extract_hostile_name_rows_returns_none_without_red_icons():
    image = Image.new("RGB", (180, 100), color=(12, 13, 13))

    assert extract_hostile_name_rows(image) is None


def test_hostile_icons_and_rows_scale_with_a_4k_style_member_list():
    image = member_list_fixture().resize((360, 200), Image.Resampling.NEAREST)

    icons = find_hostile_icons(image)
    rows = extract_hostile_name_rows(image)

    assert len(icons) == 2
    assert [(icon.width, icon.height) for icon in icons] == [(22, 22), (22, 22)]
    assert rows is not None
    assert rows.size == (322, 96)


def test_row_height_does_not_scale_with_a_wide_capture_region():
    source = member_list_fixture()
    image = Image.new("RGB", (480, source.height), color=(12, 13, 13))
    image.paste(source, (0, 0))

    rows = extract_hostile_name_rows(image)

    assert rows is not None
    assert rows.size == (461, 48)
