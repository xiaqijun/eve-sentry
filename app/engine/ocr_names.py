"""OCR member-list name cleanup."""

import re

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
