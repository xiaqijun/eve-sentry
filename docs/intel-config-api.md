# EVE Sentry Intel Config API

> Date: 2026-06-30

The intel server can load and update scoring rules from `intel_config.json`.
The default server entrypoint enables this store automatically.

## Start Server

```powershell
uv run python -m app.server --host 127.0.0.1 --port 8765 --config intel_config.json
```

SQLite mode can use the same config file:

```powershell
uv run python -m app.server --storage sqlite --db intel.sqlite3 --config intel_config.json
```

## GET /api/config

Returns the active scoring config.

```json
{
  "config": {
    "whitelist": [],
    "blacklist": [],
    "hostile_corporation_ids": [],
    "hostile_alliance_ids": [],
    "hostile_standing_threshold": -5.0,
    "cooldown_seconds": 60.0
  }
}
```

## PUT /api/config

Updates one or more config fields. Updates are persisted and applied
immediately; existing alert cache is cleared so future `/api/alerts` and
`/api/events` responses use the new rules.

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

## Runtime Data

Do not commit local config files. `intel_config.json` is ignored by git.
