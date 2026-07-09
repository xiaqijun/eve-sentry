# EVE Sentry Intel Config API

> Date: 2026-07-01
> Current workflow baseline (2026-07-09): 第一版配置语义从 scoring 转为
> classification。服务端只在角色被分类为白名或红名时触发一次性告警；`score`、
> `min_score` 和 `min_level` 属于旧兼容字段。

The intel server can load and update classification rules from `intel_config.json`.
The default server entrypoint enables this store automatically.

## Start Server

```powershell
uv run python -m app.server --host 127.0.0.1 --port 8765 --config intel_config.json
```

Production deployments can use PostgreSQL:

```powershell
uv run python -m app.server --storage postgres --postgres-dsn postgresql://eve_sentry:secret@127.0.0.1:5432/eve_sentry --config intel_config.json
```

SQLite remains available for local development. Use `--db` to choose the database path:

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

## GET /api/v1/config

Returns the active classification config. Existing deployments may still expose
legacy `scoring_*` fields during migration, but new design should consume
classification rules and alert reasons.

```json
{
  "config": {
    "schema_version": "classification_config.v1",
    "classification_version": "classification.v1",
    "defaults": {
      "source": "builtin",
      "friendly_standing_threshold": 5.0,
      "hostile_standing_threshold": -5.0,
      "cooldown_seconds": 60.0
    },
    "classification_rules": [
      {"reason": "manual_red", "classification": "red", "source": "builtin"},
      {"reason": "manual_white", "classification": "white", "source": "builtin"}
    ],
    "whitelist": [],
    "blacklist": [],
    "friendly_corporation_ids": [],
    "friendly_alliance_ids": [],
    "hostile_corporation_ids": [],
    "hostile_alliance_ids": [],
    "friendly_standing_threshold": 5.0,
    "hostile_standing_threshold": -5.0,
    "alert_once": true
  }
}
```

`classification_rules` is a compact registry of known classification reasons.
zKillboard/killboard evidence is not part of the current server classification
path.

## PUT /api/v1/config

Updates one or more config fields. Updates are persisted and applied
immediately; existing alert cache is cleared so future `/api/v1/alerts` and
`/api/v1/events` responses use the new rules.

```json
{
  "whitelist": ["Friendly Pilot"],
  "blacklist": ["Known Hostile"],
  "friendly_corporation_ids": [98000002],
  "friendly_alliance_ids": [99000002],
  "hostile_corporation_ids": [98000001],
  "hostile_alliance_ids": [99000001],
  "friendly_standing_threshold": 5.0,
  "hostile_standing_threshold": -5.0,
  "alert_once": true
}
```

Set `friendly_standing_threshold` to `null` to disable automatic white
classification from authenticated ESI contacts. Set `hostile_standing_threshold`
to `null` to disable standing-based red classification.

`whitelist` classifies targets by pilot name as white. `friendly_corporation_ids`
and `friendly_alliance_ids` classify targets as white after ESI resolves the
pilot profile.
When authenticated ESI contacts are enabled, a character, corporation, or
alliance contact whose standing is at or above `friendly_standing_threshold`
also classifies that target as white automatically. `blacklist`,
`hostile_corporation_ids`, `hostile_alliance_ids`, and hostile standings classify
targets as red. White and red classifications both create a one-time alert.
Neutral and unknown targets are kept as observations but do not create alerts.

## Related Intel APIs

Classification config affects alert generation and event streaming. The broader intel
API surface is documented in `docs/intel-platform-architecture.md`; the active
server currently includes:

- `POST /api/observations` and legacy `POST /api/intel` for observation intake.
- `POST /api/v1/channel-lines` for server-side intel channel parsing.
- `GET /api/health` for local integration health, including storage, config,
  ESI, and event-stream status. The `killboard` health key is retained only as
  a disabled compatibility field.
- `GET /api/v1/alerts`, `GET /api/v1/alerts/{id}`, and `POST /api/v1/alerts/{id}/ack`.
- `GET /api/v1/events` for SSE alert streaming with the same alert filters.
- `GET /api/intel/character/{character_id}`, `/api/intel/system/{system_id}`,
  `/api/intel/corporation/{corporation_id}`, and `/api/intel/alliance/{alliance_id}`
  for entity-centered observations, alerts, counts, and filters.
- `GET /api/v1/esi/status`, `GET /api/v1/esi/session`, and
  `GET/POST /api/v1/esi/login` for authenticated ESI state and browser-started SSO.
- `GET /api/v1/kill-activity/...` is retained as a disabled compatibility route
  and returns 404 while killboard enrichment is removed.

Compatibility note: legacy `/api/config`, `/api/channel-lines`, `/api/alerts`, and `/api/events` routes remain available for old clients during migration; new clients and docs should use `/api/v1/config`, `/api/v1/channel-lines`, `/api/v1/alerts`, and `/api/v1/events`.

## Runtime Data

Do not commit local config files. `intel_config.json`, ESI token files,
SQLite databases, ESI caches, and other runtime state should stay local.
The local startup flow and runtime file list are documented in
`docs/local-integration.md`.
