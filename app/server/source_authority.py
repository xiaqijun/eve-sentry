"""Deterministic first-resident selection over persisted detector evidence."""

from typing import Any


def is_resident_presence(item: Any) -> bool:
    """A valid zero frame retains residency; departure/expiry does not."""
    return bool(
        item.source == "eve-sentry-detector"
        and item.metadata.get("presence_only")
        and not item.metadata.get("left_reason")
        and (item.active or item.metadata.get("hostile_icon_count") == 0)
    )


def next_join_order(items: Any) -> int:
    """Assign arrival order under the store lock, never from a client clock."""
    return 1 + max(
        (int(item.metadata.get("source_join_order") or 0) for item in items), default=0
    )


def primary_sources(items: Any) -> dict[str, Any]:
    """Choose the oldest continuous resident, including its confirmed clear."""
    sources: dict[str, Any] = {}

    def rank(item):
        # Legacy rows precede newly joined nodes. Their stored first frame is
        # the only migration ordering evidence available; ties are stable.
        order = int(item.metadata.get("source_join_order") or 0)
        return (
            order,
            str(item.first_seen_at if not order else ""),
            str(item.metadata.get("client_id") or ""),
        )

    for item in items:
        if not is_resident_presence(item):
            continue
        key = item.system_name.casefold()
        if key not in sources or rank(item) < rank(sources[key]):
            sources[key] = item
    return sources


def authoritative_items(items: Any) -> tuple[list[Any], dict[str, Any]]:
    """Retain manual inputs and only the selected detector's active rows."""
    all_items = list(items)
    primaries = primary_sources(all_items)
    known_systems = {
        item.system_name.casefold()
        for item in all_items
        if item.source == "eve-sentry-detector" and item.metadata.get("presence_only")
    }
    selected = []
    for item in all_items:
        if item.metadata.get("query_only"):
            continue
        key = item.system_name.casefold()
        if item.source != "eve-sentry-detector" or key not in known_systems:
            selected.append(item)
            continue
        primary = primaries.get(key)
        if primary is None or not primary.active:
            continue
        if item.metadata.get("client_id") == primary.metadata.get("client_id"):
            selected.append(item)
    return selected, primaries
