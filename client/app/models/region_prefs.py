"""Persist the manually selected member-list region."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path


logger = logging.getLogger(__name__)
REGION_PREFERENCES_FILENAME = "region_prefs.json"


def _user_state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "EVE Sentry"
    return Path.home() / ".eve-sentry"


def default_region_preferences_path() -> Path:
    """Return the update-safe per-user path for capture region settings."""
    return _user_state_root() / REGION_PREFERENCES_FILENAME


def _legacy_region_preferences_paths() -> tuple[Path, ...]:
    """Return old locations that may still contain update-sensitive settings."""
    candidates = [
        _user_state_root()
        / "updates"
        / "previous-version"
        / REGION_PREFERENCES_FILENAME,
    ]
    if getattr(sys, "frozen", False):
        candidates.append(
            Path(sys.executable).resolve().parent / REGION_PREFERENCES_FILENAME
        )
    candidates.append(Path.cwd() / REGION_PREFERENCES_FILENAME)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return tuple(unique)


def _read_preferences(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class RegionPreferences:
    """Store the member-list region as ratios relative to the EVE window."""

    def __init__(self, filepath: str | Path | None = None) -> None:
        use_default_path = filepath is None
        self._filepath = (
            default_region_preferences_path()
            if use_default_path
            else Path(filepath)
        )
        target_existed = self._filepath.exists()
        self._data = self._load()
        if use_default_path and not target_existed:
            self._migrate_legacy_preferences()

    def _load(self) -> dict:
        return _read_preferences(self._filepath) or {}

    def _migrate_legacy_preferences(self) -> None:
        target = self._filepath.resolve()
        for candidate in _legacy_region_preferences_paths():
            if candidate.resolve() == target:
                continue
            data = _read_preferences(candidate)
            if data is None:
                continue
            self._data = data
            try:
                self._save()
            except OSError:
                logger.warning(
                    "Could not migrate region preferences from %s to %s",
                    candidate,
                    self._filepath,
                    exc_info=True,
                )
                return
            logger.info(
                "Migrated region preferences from %s to %s",
                candidate,
                self._filepath,
            )
            return

    def _save(self) -> None:
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
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

        normalized = {
            "x_ratio": max(0.0, min(1.0, x_ratio)),
            "y_ratio": max(0.0, min(1.0, y_ratio)),
            "w_ratio": max(0.0, min(1.0, w_ratio)),
            "h_ratio": max(0.0, min(1.0, h_ratio)),
        }
        self._data["member_list_region"] = normalized
        regions = self._data.setdefault("member_list_regions", {})
        if isinstance(regions, dict):
            window_key = self._window_key(window)
            regions[window_key] = normalized
            title_key = self._legacy_window_key(window)
            if title_key != window_key:
                regions[title_key] = normalized
        self._save()

    def resolve_region(self, window: dict) -> dict | None:
        """Return the saved region mapped onto the current window bounds."""
        stored = self._region_for_window(window)
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

    def _region_for_window(self, window: dict) -> dict | None:
        """Return a saved region for the exact window, falling back to legacy data."""
        regions = self._data.get("member_list_regions")
        if isinstance(regions, dict):
            stored = regions.get(self._window_key(window))
            if isinstance(stored, dict):
                return stored
            legacy_stored = regions.get(self._legacy_window_key(window))
            if isinstance(legacy_stored, dict):
                return legacy_stored
            title_key = self._legacy_window_key(window)
            title_suffix = f":{title_key}"
            title_matches = [
                item
                for key, item in regions.items()
                if isinstance(item, dict)
                and isinstance(key, str)
                and key.endswith(title_suffix)
            ]
            if len(title_matches) == 1:
                return title_matches[0]
            if regions:
                return None
        stored = self._data.get("member_list_region")
        return stored if isinstance(stored, dict) else None

    def _window_key(self, window: dict) -> str:
        """Return a stable-enough key for saving per-window region preferences."""
        hwnd = window.get("hwnd")
        title = str(window.get("title") or "").strip()
        if hwnd not in {None, ""}:
            title_key = title.casefold()
            return f"hwnd:{hwnd}:{title_key}" if title_key else f"hwnd:{hwnd}"
        if title:
            return title.casefold()
        return "default"

    def _legacy_window_key(self, window: dict) -> str:
        """Return the pre-hwnd per-window key for backward compatibility."""
        title = str(window.get("title") or "").strip()
        if title:
            return title.casefold()
        hwnd = window.get("hwnd")
        if hwnd not in {None, ""}:
            return f"hwnd:{hwnd}"
        return "default"
