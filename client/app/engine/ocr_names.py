"""OCR member-list name cleanup."""

import re

_LEADING_ICON_RE = re.compile(r"^[^\w]+(?=\s*\w)")
_DISTANCE_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:m|km|au)\s*$",
    re.IGNORECASE,
)
_NUMERIC_NAME_SUFFIX_RE = re.compile(r"^0\d{1,2}$")


def is_plausible_ocr_name(text: str) -> bool:
    """Return whether OCR text looks like a pilot name rather than UI noise."""
    value = str(text or "").strip()
    if not value or "\ufffd" in value or "*" in value or _DISTANCE_RE.fullmatch(value):
        return False
    # EVE character names use the Latin alphabet; non-Latin OCR glyphs are
    # typically localized UI labels or replacement-character noise.
    if not re.search(r"[A-Za-z]", value):
        return False
    if re.search(r"\d\s*(?:m|km|au)\b", value, flags=re.IGNORECASE):
        return False
    return True


def _clean_ocr_name(text: str) -> str:
    """Remove OCR noise from member-list icons before pilot names."""
    cleaned = _LEADING_ICON_RE.sub("", text.strip()).strip()
    if not is_plausible_ocr_name(cleaned):
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
        for part in str(text).split(","):
            raw_part = part.strip()
            # PaddleOCR can return a valid name's zero-padded numeric suffix
            # as a separate box (for example ``STARKEY`` + ``07``). A
            # leading zero distinguishes this from member-count noise.
            if names and _NUMERIC_NAME_SUFFIX_RE.fullmatch(raw_part):
                previous = names[-1]
                seen.discard(previous.casefold())
                merged = f"{previous} {raw_part}"
                names[-1] = merged
                seen.add(merged.casefold())
                continue
            name = _clean_ocr_name(raw_part)
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
    return names
