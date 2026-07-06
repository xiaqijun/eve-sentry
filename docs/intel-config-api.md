# EVE Sentry Intel Config API

> Date: 2026-07-01

The intel server can load and update scoring rules from `intel_config.json`.
The default server entrypoint enables this store automatically.

## Start Server

```powershell
uv run python -m app.server --host 127.0.0.1 --port 8765 --config intel_config.json
```

The default server storage is SQLite. Use `--db` to choose the database path:

```powershell
uv run python -m app.server --db intel.sqlite3 --config intel_config.json
```

Legacy JSON storage remains available for compatibility:

```powershell
uv run python -m app.server --storage json --data intel_reports.json --config intel_config.json
```

To migrate old JSON data into SQLite explicitly:

```powershell
uv run python scripts/import_intel_json.py --source intel_reports.json --db intel.sqlite3 --json
```

## GET /api/config

Returns the active scoring config.

```json
{
  "config": {
    "schema_version": "scoring_config.v1",
    "scoring_version": "scoring.v1",
    "defaults": {
      "source": "builtin",
      "hostile_standing_threshold": -5.0,
      "cooldown_seconds": 60.0
    },
    "evidence_rules": [
      {"type": "local_ocr_seen", "default_weight": 40, "source": "builtin"},
      {"type": "blacklist_match", "default_weight": 80, "source": "builtin"}
    ],
    "whitelist": [],
    "blacklist": [],
    "hostile_corporation_ids": [],
    "hostile_alliance_ids": [],
    "hostile_standing_threshold": -5.0,
    "cooldown_seconds": 60.0
  }
}
```

`evidence_rules` is a compact registry of known evidence types and built-in
default weights. Some dynamic rules, such as killboard activity, use
`null` for `default_weight` because the score is computed from activity volume.

## PUT /api/config

Updates one or more config fields. Updates are persisted and applied
immediately; existing alert cache is cleared so future `/api/v1/alerts` and
`/api/v1/events` responses use the new rules.

```json
{
  "whitelist": ["Friendly Pilot"],
  "blacklist": ["Known Hostile"],
  "hostile_corporation_ids": [98000001],
  "hostile_alliance_ids": [99000001],
  "hostile_standing_threshold": -5.0,
  "cooldown_seconds": 60
}
```

Set `hostile_standing_threshold` to `null` to disable standing-based evidence.

## Related Intel APIs

Scoring config affects alert generation and event streaming. The broader intel
API surface is documented in `docs/intel-platform-architecture.md`; the active
server currently includes:

- `POST /api/observations` and legacy `POST /api/intel` for observation intake.
- `POST /api/channel-lines` for server-side intel channel parsing.
- `GET /api/health` for local integration health, including storage, config,
  ESI, killboard, and event-stream status.
- `GET /api/v1/alerts`, `GET /api/v1/alerts/{id}`, and `POST /api/v1/alerts/{id}/ack`.
- `GET /api/v1/events` for SSE alert streaming with the same alert filters.
- `GET /api/intel/character/{character_id}`, `/api/intel/system/{system_id}`,
  `/api/intel/corporation/{corporation_id}`, and `/api/intel/alliance/{alliance_id}`
  for entity-centered observations, alerts, activity, counts, and filters.
- `GET /api/v1/esi/status`, `GET /api/v1/esi/session`, and
  `GET/POST /api/v1/esi/login` for authenticated ESI state and browser-started SSO.
- `GET /api/v1/kill-activity/...` for character, system, corporation, and alliance activity.

Compatibility note: legacy `/api/alerts` and `/api/events` routes remain available for old clients during migration; new clients and docs should use `/api/v1/alerts` and `/api/v1/events`.

## Runtime Data

Do not commit local config files. `intel_config.json`, ESI token files,
SQLite databases, zKillboard caches, and other runtime state should stay local.
The local startup flow and runtime file list are documented in
`docs/local-integration.md`.
