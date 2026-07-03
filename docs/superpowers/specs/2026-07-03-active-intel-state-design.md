# Active Intel State Design

## Goal

Add a realtime intel state layer that can remove stale realtime threats without deleting historical observations. Historical `observations` remain the audit trail. Realtime `active_intel` becomes the source for current workbench panels, alert freshness, map badges, and pilot observation lists.

## Problem

The current server mainly stores observations as historical events. This is correct for audit and later analysis, but it is not enough for realtime UI:

- OCR may report the same visible pilot every 2 seconds.
- OCR may miss a pilot for one frame even though the pilot is still visible.
- Intel channel reports remain risky for a short period, then become stale.
- Channel clear messages such as `clr`, `clear`, `安全`, or `清了` should remove realtime threat state.
- The frontend should show what is currently relevant, not every historical observation.

## Core Model

Keep three separate concepts:

```text
observations
  Historical immutable-ish intel records.
  Used for audit, timeline, scoring inputs, and detail views.

active_intel
  Current realtime state derived from observations and snapshots.
  Used for map badges, pilot observation list, realtime threat panel, and current risk.

alerts
  Notification events generated from active intel and scoring.
  Used for user attention and acknowledgement.
```

## Active Intel Fields

Initial server-side representation:

```text
id
source
source_instance
system_name
system_id
target_type
name
character_id
raw_text
metadata
first_seen_at
last_seen_at
expires_at
left_at
cleared_at
active
seen_count
confidence
source_observation_ids
```

`source_observation_ids` links realtime state back to historical observations without duplicating frontend rows.

## OCR State Rules

OCR is treated as a current visible snapshot.

Client behavior:

```text
Every 2 seconds:
  capture local member list
  OCR and clean pilot names
  upload only the detected pilot-name list plus minimal context
```

Server behavior:

```text
For each name in uploaded snapshot:
  if active record exists for client + system + name:
    refresh last_seen_at
    increment seen_count
  else:
    create active record
    create historical observation

For active OCR records missing from latest snapshot:
  keep active during grace period
  mark inactive after grace period
```

Default OCR grace period:

```text
6 seconds
```

Reason: with a 2-second scan interval, this tolerates about 3 missed OCR scans before removing the pilot from realtime state.

OCR missing behavior:

```text
last_seen_at = 12:00:00
12:00:02 snapshot missing pilot -> still active
12:00:04 snapshot missing pilot -> still active
12:00:08 snapshot missing pilot -> active=false, left_at=12:00:08
```

## Channel State Rules

Intel channel messages are treated as time-limited threat claims, not current visible snapshots.

Default TTL:

```text
same-system hostile report: 10 minutes
adjacent-system or gate movement: 5 minutes
hostile count only: 3 minutes
fleet or camp signal: 15 minutes
bridge or staging signal: 20 minutes
```

When a channel observation is parsed:

```text
if it contains hostile intel:
  create or refresh active channel state
  set expires_at from rule-specific TTL
  keep historical observation

if it contains clear language:
  clear matching active channel state
  keep clear observation as historical record
```

Clear words:

```text
clr
clear
clean
安全
清了
已清
走了
没了
散了
```

Clear scope:

```text
clear message with system:
  clear active channel state for that system

clear message with gate/direction:
  clear matching direction metadata if present

clear message without system:
  store as historical observation only
  do not globally clear all state
```

## Frontend Behavior

Realtime panels consume `active_intel`.

Historical panels consume `observations`.

Suggested UI split:

```text
Enemy Pilot Observation List
  source: active_intel
  shows OCR visible pilots, active channel threats, zKill/ESI-derived current risk

History / Timeline
  source: observations
  shows all historical records, including inactive and cleared entries

Alert Queue
  source: alerts
  shows generated alert events and acknowledgement state
```

Inactive realtime records should disappear from the default active list. Detail views may show recently inactive records if the user enables a history filter.

## API Shape

Add endpoints:

```text
GET  /api/v1/active-intel
POST /api/v1/ocr/snapshot
POST /api/v1/channel-lines
```

`POST /api/v1/ocr/snapshot` payload:

```json
{
  "client_id": "detector-client:123",
  "source_instance": "EVE - Hajimi6",
  "system_name": "S-KSWL",
  "system_id": null,
  "scan_interval_seconds": 2,
  "seen_at": "2026-07-03T10:00:00+00:00",
  "names": ["Alice", "Bob"]
}
```

The client does not send enter/leave decisions, cache state, active flags, expiry
timestamps, or deletion requests. The server derives all active/inactive state by
comparing the latest submitted `names` list with the previous server-side state
for the same client/window/system context.

Response:

```json
{
  "ok": true,
  "created": 1,
  "refreshed": 1,
  "missing": 0,
  "expired": 0,
  "active": []
}
```

`GET /api/v1/active-intel` response:

```json
{
  "active_intel": [],
  "count": 0,
  "generated_at": "2026-07-03T10:00:00+00:00"
}
```

## Storage Approach

First implementation can keep active state in the same store abstraction:

```text
IntelStore._active_intel
SQLiteIntelStore active_intel table
```

This avoids overloading historical `IntelReport` rows with current-state semantics.

SQLite table:

```sql
CREATE TABLE IF NOT EXISTS active_intel (
  active_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_instance TEXT NOT NULL,
  system TEXT NOT NULL,
  system_id INTEGER,
  target_type TEXT NOT NULL,
  name TEXT NOT NULL,
  character_id INTEGER,
  raw_text TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL DEFAULT '',
  left_at TEXT NOT NULL DEFAULT '',
  cleared_at TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  seen_count INTEGER NOT NULL DEFAULT 1,
  confidence REAL,
  source_observation_ids_json TEXT NOT NULL DEFAULT '[]'
);
```

## Scoring And Alerts

Alerts should be generated when active intel becomes newly risky, not on every refresh.

OCR refresh:

```text
existing active pilot refreshed -> no new alert
new active pilot created -> scoring may create alert
pilot inactive then reappears after grace/window -> scoring may create alert
```

Channel refresh:

```text
same channel threat refreshed -> no new alert unless severity increases
new system/name threat -> scoring may create alert
clear message -> no threat alert, but may create low-priority clear event later
```

## Open Defaults

Initial defaults:

```text
OCR scan interval: 2 seconds
OCR grace period: 6 seconds
Channel same-system TTL: 10 minutes
Channel adjacent/gate TTL: 5 minutes
Hostile count-only TTL: 3 minutes
Fleet/camp TTL: 15 minutes
Bridge/staging TTL: 20 minutes
```

These should live in server config and be exposed by `GET /api/v1/config`.

## Acceptance Criteria

- Historical observations remain available after realtime state expires.
- OCR repeated snapshots refresh active rows instead of inserting duplicate realtime rows.
- OCR missing pilots remain active during grace period.
- OCR missing pilots become inactive after grace period.
- Channel threats expire by TTL.
- Channel clear messages deactivate matching active channel state.
- Clear messages without a system do not clear all state.
- Frontend active list reads active intel, not raw historical observations.
- Alert generation does not repeat for every OCR refresh.
