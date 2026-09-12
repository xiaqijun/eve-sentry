"""Transactional system projection of the committed hostile event stream."""

import json
from typing import Any

# Keep the established, normalized EVE system-name event key. EVE systems do
# not rename; system_id remains in the payload, including unmapped inputs.
SCHEMA = """
CREATE TABLE IF NOT EXISTS system_current_state (
    system_key TEXT PRIMARY KEY,
    state_version BIGINT NOT NULL CHECK (state_version > 0),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

BACKFILL = """
INSERT INTO system_current_state (
    system_key, state_version, event_type, occurred_at, payload_json
)
SELECT DISTINCT ON (entity_key)
       entity_key, seq, event_type, occurred_at, payload_json
FROM intel_events
WHERE event_type IN ('alert.entered', 'alert.updated', 'alert.cleared')
ORDER BY entity_key, seq DESC
ON CONFLICT (system_key) DO UPDATE SET
    state_version = EXCLUDED.state_version,
    event_type = EXCLUDED.event_type,
    occurred_at = EXCLUDED.occurred_at,
    payload_json = EXCLUDED.payload_json,
    updated_at = NOW()
WHERE system_current_state.state_version < EXCLUDED.state_version
"""

# Allocation, event insertion and projection advance share the writer's
# transaction/advisory lock. A duplicate event cannot advance the projection.
APPEND_EVENT = """
WITH incoming(event_key, event_type, entity_key, occurred_at, payload_json) AS (
    VALUES (?::text, ?::text, ?::text, ?::text, ?::text)
), inserted AS (
    INSERT INTO intel_events (
        event_key, event_type, entity_key, occurred_at, payload_json
    )
    SELECT incoming.* FROM incoming
    WHERE NOT EXISTS (
        SELECT 1 FROM system_current_state AS current
        WHERE current.system_key = incoming.entity_key
          AND current.payload_json::jsonb = incoming.payload_json::jsonb
    )
    ON CONFLICT (event_key) DO NOTHING
    RETURNING seq, event_type, entity_key, occurred_at, payload_json
)
INSERT INTO system_current_state (
    system_key, state_version, event_type, occurred_at, payload_json
)
SELECT entity_key, seq, event_type, occurred_at, payload_json
FROM inserted
WHERE event_type IN ('alert.entered', 'alert.updated', 'alert.cleared')
ON CONFLICT (system_key) DO UPDATE SET
    state_version = EXCLUDED.state_version,
    event_type = EXCLUDED.event_type,
    occurred_at = EXCLUDED.occurred_at,
    payload_json = EXCLUDED.payload_json,
    updated_at = NOW()
WHERE system_current_state.state_version < EXCLUDED.state_version
"""


def migrate_system_state(connection: Any) -> None:
    """Add/backfill the projection without altering reports or event history."""
    connection.execute(SCHEMA)
    connection.execute(BACKFILL)


def project_active_items(
    raw_items: list[dict[str, Any]], states: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Adapt committed results to v1 active-intel without reclassifying rows.

    Raw observations remain available through history APIs. Unmigrated systems
    retain their previous representation until their first durable state event.
    A clear projection deliberately hides all older raw rows for that system.
    """
    covered = {str(row["system_key"]).casefold() for row in states}
    items = [
        item
        for item in raw_items
        if str(item.get("system_name") or "").casefold() not in covered
    ]
    for row in states:
        payload = json.loads(row["payload_json"])
        count = max(0, int(payload.get("hostile_count") or 0))
        if (not count or not payload.get("active", True)) and payload.get(
            "freshness"
        ) != "unknown":
            continue
        system_key = str(row["system_key"])
        timestamp = str(row["occurred_at"])
        common = {
            "source": "eve-sentry-detector",
            "source_instance": "system_current_state",
            "system_name": payload.get("system_name") or system_key,
            "system_id": payload.get("system_id"),
            "active": True,
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "seen_count": 1,
            "source_observation_ids": [],
            "state_version": int(row["state_version"]),
        }
        metadata = {
            "client_id": "system_current_state",
            "system_state": True,
            "state_version": int(row["state_version"]),
            "hostile_icon_count": count,
            "hostile_icon_seen_at": timestamp,
            "freshness": str(payload.get("freshness") or "fresh"),
            "primary_client_id": str(payload.get("primary_client_id") or ""),
            "primary_generation": int(payload.get("primary_generation") or 0),
        }
        items.append(
            {
                **common,
                "id": f"system:{system_key}:presence",
                "target_type": "system",
                "name": "",
                "character_id": None,
                "metadata": {**metadata, "presence_only": True},
            }
        )
        for person in payload.get("hostile_personnel") or []:
            items.append(
                {
                    **common,
                    "id": f"system:{system_key}:character:{person['character_id']}",
                    "target_type": "character",
                    "name": person["name"],
                    "character_id": person["character_id"],
                    "first_seen_at": person.get("first_seen_at") or timestamp,
                    # This is already the server-classified result, not another
                    # request to run current classifier settings over old rows.
                    "metadata": {
                        **metadata,
                        "identity_status": "resolved",
                        "hostile_count": 1,
                    },
                }
            )
    return items
