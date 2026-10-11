"""Deterministic first-resident selection over persisted detector evidence."""

from typing import Any


def source_identity_from_metadata(metadata: Any, client_id: str = "") -> str:
    """Build the internal source scope from a row's server-owned metadata."""
    metadata = metadata if isinstance(metadata, dict) else {}
    account_id = str(metadata.get("_server_seat_account_id") or "").strip()
    key_id = str(metadata.get("_server_seat_key_id") or "").strip()
    client_id = str(metadata.get("client_id") or client_id or "").strip()
    if account_id or key_id:
        return f"seat:{account_id}:{key_id}:{client_id}"
    return client_id


def source_identity(item: Any) -> str:
    """Return the authenticated identity of a detector source.

    ``client_id`` is a local runtime identifier and can be reused after a
    reinstall.  Once the server has attributed a presence to a Seat account
    and key, include that attribution in the source identity so two clients
    with the same local id cannot overwrite one another's authority rows.
    Legacy, unattributed rows continue to use their client id unchanged.
    """
    metadata = item.metadata if isinstance(getattr(item, "metadata", None), dict) else {}
    return source_identity_from_metadata(metadata)


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


def superseded_parent_clients(items: Any) -> set[str]:
    """Per-window Presence retires only its exact legacy machine identity.

    Departed windows still prove migration: stopping them must not resurrect a
    machine's old zero frame. Query rows are not monitoring-window evidence.
    """
    parents = set()
    for item in items:
        if (item.source != "eve-sentry-detector"
                or not item.metadata.get("presence_only")
                or item.metadata.get("query_only")):
            continue
        client_id = str(item.metadata.get("client_id") or "")
        parent, marker, window = client_id.partition(":user-")
        if parent and marker and window and ":" not in window:
            parents.add(parent)
    return parents


def primary_sources(items: Any) -> dict[str, Any]:
    """Choose the oldest continuous resident, including its confirmed clear."""
    sources: dict[str, Any] = {}
    items = list(items)
    retired = superseded_parent_clients(items)

    def rank(item):
        # Legacy rows precede newly joined nodes. Their stored first frame is
        # the only migration ordering evidence available; ties are stable.
        order = int(item.metadata.get("source_join_order") or 0)
        return (
            order,
            str(item.first_seen_at if not order else ""),
            source_identity(item),
        )

    for item in items:
        if (not is_resident_presence(item)
                or item.metadata.get("client_id") in retired):
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
        if source_identity(item) == source_identity(primary):
            selected.append(item)
    return selected, primaries
