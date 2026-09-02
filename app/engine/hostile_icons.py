"""Detect hostile standing icons in a captured member-list image."""

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
