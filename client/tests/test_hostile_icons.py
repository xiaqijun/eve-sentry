from PIL import Image, ImageDraw

from app.engine.hostile_icons import find_hostile_icons


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


def test_hostile_icons_scale_with_a_4k_style_member_list():
    image = member_list_fixture().resize((360, 200), Image.Resampling.NEAREST)

    icons = find_hostile_icons(image)

    assert len(icons) == 2
    assert [(icon.width, icon.height) for icon in icons] == [(22, 22), (22, 22)]
