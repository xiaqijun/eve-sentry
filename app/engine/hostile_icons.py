"""Detect hostile standing icons and isolate their member-list rows."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class HostileIcon:
    """Bounding box for one red hostile icon."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom - 1) / 2


def find_hostile_icons(image: Image.Image) -> list[HostileIcon]:
    """Return red square standing icons near the left edge of a member list."""
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    if pixels.size == 0:
        return []

    red = pixels[:, :, 0]
    green = pixels[:, :, 1]
    blue = pixels[:, :, 2]
    red_mask = (red >= 100) & ((red - green) >= 60) & ((red - blue) >= 60)

    _height, width = red_mask.shape
    scale = _member_list_scale(width)
    minimum_icon_size = 6
    maximum_icon_size = max(18, int(round(18 * scale)))
    search_width = min(width, max(24, int(round(width * 0.35))))
    left_mask = red_mask[:, :search_width]
    active_rows = left_mask.sum(axis=1) >= 3

    icons: list[HostileIcon] = []
    for top, bottom in _true_runs(active_rows):
        _ys, xs = np.nonzero(left_mask[top:bottom])
        if not len(xs):
            continue
        left = int(xs.min())
        right = int(xs.max()) + 1
        box_height = bottom - top
        box_width = right - left
        if not (
            minimum_icon_size <= box_width <= maximum_icon_size
            and minimum_icon_size <= box_height <= maximum_icon_size
        ):
            continue
        if not 0.65 <= box_width / box_height <= 1.35:
            continue
        red_pixels = int(left_mask[top:bottom, left:right].sum())
        if red_pixels < max(20, int(box_width * box_height * 0.35)):
            continue
        icons.append(HostileIcon(left, top, right, bottom))
    return icons


def extract_hostile_name_rows(
    image: Image.Image,
    icons: list[HostileIcon] | None = None,
) -> Image.Image | None:
    """Stack only red-icon rows into a compact image suitable for OCR."""
    if icons is None:
        icons = find_hostile_icons(image)
    if not icons:
        return None

    rows = extract_hostile_name_row_images(image, icons)
    if not rows:
        return None

    scale = _hostile_icon_scale(icons)
    padding = max(6, int(round(6 * scale)))
    separator = max(4, int(round(4 * scale)))
    row_width, row_height = rows[0].size
    output_height = padding * 2 + len(rows) * row_height
    output_height += max(0, len(rows) - 1) * separator
    output = Image.new("RGB", (row_width, output_height), color=(0, 0, 0))

    for index, row in enumerate(rows):
        output_y = padding + index * (row_height + separator)
        output.paste(row, (0, output_y))
    return output


def extract_hostile_name_row_images(
    image: Image.Image,
    icons: list[HostileIcon] | None = None,
) -> list[Image.Image]:
    """Return one padded name crop for each hostile icon, in screen order.

    Keeping each row in its own image prevents OCR text detection from
    inventing line breaks inside a pilot name (for example ``STARKEY 07``).
    """
    if icons is None:
        icons = find_hostile_icons(image)
    if not icons:
        return []

    icons = sorted(icons, key=lambda icon: (icon.top, icon.left))
    source = image.convert("RGB")
    scale = _hostile_icon_scale(icons)
    name_left = min(
        source.width,
        max(icon.right for icon in icons) + max(2, int(round(2 * scale))),
    )
    output_width = source.width - name_left
    if output_width <= 0:
        return []

    row_height = max(
        int(round(16 * scale)),
        max(icon.height for icon in icons) + int(round(5 * scale)),
    )
    rows: list[Image.Image] = []
    for index, icon in enumerate(icons):
        requested_top = int(round(icon.center_y - row_height / 2))
        requested_bottom = requested_top + row_height
        previous_boundary = (
            int(round((icons[index - 1].center_y + icon.center_y) / 2))
            if index > 0
            else 0
        )
        next_boundary = (
            int(round((icon.center_y + icons[index + 1].center_y) / 2)) + 1
            if index + 1 < len(icons)
            else source.height
        )
        source_top = max(0, requested_top, previous_boundary)
        source_bottom = min(source.height, requested_bottom, next_boundary)
        if source_bottom <= source_top:
            continue
        row = source.crop((name_left, source_top, source.width, source_bottom))
        padded = Image.new("RGB", (output_width, row_height), color=(0, 0, 0))
        padded.paste(row, (0, source_top - requested_top))
        rows.append(padded)
    return rows


def _hostile_icon_scale(icons: list[HostileIcon]) -> float:
    """Estimate UI scaling from the standing icon, not user-selected width."""
    heights = sorted(icon.height for icon in icons)
    median_height = heights[len(heights) // 2]
    return max(1.0, min(2.5, float(median_height) / 11.0))


def _member_list_scale(width: int) -> float:
    """Estimate EVE UI scaling from the physical member-list width."""
    return max(1.0, min(2.5, float(width) / 180.0))


def _true_runs(values: np.ndarray) -> list[tuple[int, int]]:
    """Return half-open ranges for contiguous true values."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, enabled in enumerate(values.tolist()):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(values)))
    return runs
