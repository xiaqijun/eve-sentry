"""Threat detection: compare OCR results against a whitelist."""

import time

from app.models.whitelist import Whitelist


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
            name = text.strip()
            if not name:
                continue
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
