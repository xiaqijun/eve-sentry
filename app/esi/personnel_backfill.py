"""Bounded, restartable imports of confirmed identities, never fuzzy OCR guesses."""

from __future__ import annotations

import json
from datetime import datetime

from app.esi.personnel_archive import IdentityUpdate, NameConflict


class PersonnelBackfill:
    def __init__(self, archive, legacy_cache=None):
        self.archive = archive
        self.legacy_cache = legacy_cache

    def step(self, source="history", limit=100):
        if source not in {"history", "legacy"} or not 1 <= limit <= 1000:
            raise ValueError("invalid backfill source or batch size")
        with self.archive._connection() as connection:
            connection.execute(
                "INSERT INTO personnel_backfill_progress(source) VALUES (%s) ON CONFLICT (source) DO NOTHING", (source,),
            )
            progress = connection.execute(
                "SELECT * FROM personnel_backfill_progress WHERE source = %s" + self.archive._row_lock, (source,),
            ).fetchone()
            cursor = progress["cursor_value"]
            if source == "history":
                rows = connection.execute(
                    "SELECT stream_position, character_ids_json, metadata_json, seen_at FROM intel_reports "
                    "WHERE stream_position > %s ORDER BY stream_position LIMIT %s", (int(cursor or 0), limit),
                ).fetchall()
                batches = []
                for row in rows:
                    metadata = json.loads(row["metadata_json"] or "{}")
                    ids = set(json.loads(row["character_ids_json"] or "[]"))
                    confirmed = (metadata.get("identity_status") == "resolved"
                                 or (metadata.get("esi_resolution") or {}).get("resolved_character_count", 0) > 0)
                    try:
                        seen_at = datetime.fromisoformat(row["seen_at"].replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        seen_at = 0
                    profiles = metadata.get("character_profiles") or []
                    updates = [IdentityUpdate(item["character_id"], item["name"], 0, seen_at)
                               for item in profiles if isinstance(item, dict) and confirmed
                               and item.get("character_id") in ids and item.get("name")]
                    batches.append((str(row["stream_position"]), updates))
            else:
                if self.legacy_cache is None:
                    return 0
                with self.legacy_cache._lock:
                    keys = sorted(key for key in self.legacy_cache._items if key.startswith("name:") and key > cursor)[:limit]
                    entries = [(key, dict(self.legacy_cache._items[key])) for key in keys]
                batches = []
                for key, entry in entries:
                    value = entry.get("value") or {}
                    updates = [IdentityUpdate(value.get("id"), value.get("name"), 0, 0)] if (
                        isinstance(value, dict) and value.get("category") == "character") else []
                    batches.append((key, updates))
            imported = rejected = 0
            for cursor, updates in batches:
                for update in updates:
                    connection.execute("SAVEPOINT personnel_import")
                    try:
                        imported += int(self.archive._identity(connection, update))
                    except (ValueError, NameConflict):
                        connection.execute("ROLLBACK TO SAVEPOINT personnel_import")
                        rejected += 1
                    finally:
                        connection.execute("RELEASE SAVEPOINT personnel_import")
            connection.execute(
                "UPDATE personnel_backfill_progress SET cursor_value = %s, imported = imported + %s, "
                "rejected = rejected + %s WHERE source = %s", (cursor, imported, rejected, source),
            )
        return len(batches)
