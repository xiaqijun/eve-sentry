"""Threat detection: compare OCR results against a whitelist."""

import re
import time

from app.models.whitelist import Whitelist

_LEADING_ICON_RE = re.compile(r"^[^\w]+(?=\s*\w)")


def _clean_ocr_name(text: str) -> str:
    """Remove OCR noise from member-list icons before pilot names."""
    cleaned = _LEADING_ICON_RE.sub("", text.strip()).strip()
    if not re.search(r"\w", cleaned):
        return ""
    return cleaned


def _iter_ocr_names(text: str) -> list[str]:
    return [
        name
        for name in (_clean_ocr_name(part) for part in str(text).split(","))
        if name
    ]


def ocr_candidate_names(ocr_results: list[tuple[str, float]]) -> list[str]:
    """Return cleaned, de-duplicated member names from OCR result rows."""
    names: list[str] = []
    seen: set[str] = set()
    for text, _confidence in ocr_results:
        for name in _iter_ocr_names(text):
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
    return names


class Detector:
    """Compares recognised names against a whitelist and tracks
    recently-seen threats to avoid alert spam.
    """

    def __init__(
        self,
        whitelist: Whitelist,
        cooldown_seconds: float = 60.0,
    ):
        self._whitelist = whitelist
        self._cooldown = cooldown_seconds
        # name -> last-alerted timestamp
        self._last_alert: dict[str, float] = {}

    def check(self, ocr_results: list[tuple[str, float]]) -> list[str]:
        """Return names from *ocr_results* that are NOT in the whitelist
        and have not been alerted within the cooldown window.

        Each element of *ocr_results* is ``(text, confidence)``.
        """
        now = time.monotonic()
        threats: list[str] = []

        for text, _confidence in ocr_results:
            for name in _iter_ocr_names(text):
                # Skip if whitelisted
                if self._whitelist.match(name):
                    continue
                # Skip if still on cooldown
                last = self._last_alert.get(name)
                if last is not None and (now - last) < self._cooldown:
                    continue
                # New threat
                self._last_alert[name] = now
                threats.append(name)

        return threats
