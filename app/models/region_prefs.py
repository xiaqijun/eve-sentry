"""Persist the manually selected member-list region."""

from __future__ import annotations

import json
from pathlib import Path


class RegionPreferences:
    """Store the member-list region as ratios relative to the EVE window."""

    def __init__(self, filepath: str = "region_prefs.json") -> None:
        self._filepath = Path(filepath)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            if self._filepath.exists():
                data = json.loads(self._filepath.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save(self) -> None:
        self._filepath.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_region(self, window: dict, region: dict) -> None:
        """Persist the region normalized to the current window bounds."""
        if window["w"] <= 0 or window["h"] <= 0:
            return

        x_ratio = (region["x"] - window["x"]) / window["w"]
        y_ratio = (region["y"] - window["y"]) / window["h"]
        w_ratio = region["w"] / window["w"]
        h_ratio = region["h"] / window["h"]

        self._data["member_list_region"] = {
            "x_ratio": max(0.0, min(1.0, x_ratio)),
            "y_ratio": max(0.0, min(1.0, y_ratio)),
            "w_ratio": max(0.0, min(1.0, w_ratio)),
            "h_ratio": max(0.0, min(1.0, h_ratio)),
        }
        self._save()

    def resolve_region(self, window: dict) -> dict | None:
        """Return the saved region mapped onto the current window bounds."""
        stored = self._data.get("member_list_region")
        if not isinstance(stored, dict):
            return None

        try:
            x_ratio = float(stored["x_ratio"])
            y_ratio = float(stored["y_ratio"])
            w_ratio = float(stored["w_ratio"])
            h_ratio = float(stored["h_ratio"])
        except (KeyError, TypeError, ValueError):
            return None

        w = max(1, int(round(window["w"] * w_ratio)))
        h = max(1, int(round(window["h"] * h_ratio)))
        x = int(round(window["x"] + window["w"] * x_ratio))
        y = int(round(window["y"] + window["h"] * y_ratio))

        max_x = window["x"] + window["w"] - w
        max_y = window["y"] + window["h"] - h
        x = max(window["x"], min(x, max_x))
        y = max(window["y"], min(y, max_y))

        return {"x": x, "y": y, "w": w, "h": h}
