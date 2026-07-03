# Star Map Data

Date: 2026-07-01

The intel server now supports configurable star-map sources. The recommended
topology source is the official EVE SDE import, while ESI remains a profile and
identity enrichment source.

## Supported Map Sources

- `builtin`: the small bundled map used as a safe fallback
- `manual`: explicit systems and links stored in `intel_map.json`
- `sde`: import systems and jumps from a local official SDE export

## Official SDE Source

CCP provides the official SDE download and update notes here:

- docs: <https://developers.eveonline.com/docs/services/static-data/>
- automation notes: <https://developers.eveonline.com/docs/services/static-data/#automation>
- latest metadata: <https://developers.eveonline.com/static-data/tranquility/latest.jsonl>

Verified on 2026-07-01:

- latest official build: `3417089`
- release date: `2026-07-01T12:12:06Z`
- YAML package:
  `https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-3417089-yaml.zip`
- JSONL package:
  `https://developers.eveonline.com/static-data/tranquility/eve-online-static-data-3417089-jsonl.zip`

The server-side map importer is intended to use the YAML package. The metadata
endpoint can be polled to automate future refresh checks without hard-coding a
build number.

To sync the official map tables into the local runtime directory:

```powershell
python scripts/sync_sde.py --target .runtime/sde
```

## Server Startup

Start with the default bundled map:

```powershell
uv run python -m app.server --host 127.0.0.1 --port 8765
```

Start with SDE-backed topology:

```powershell
uv run python -m app.server `
  --host 127.0.0.1 `
  --port 8765 `
  --db intel.sqlite3 `
  --config intel_config.json `
  --map-config intel_map.json `
  --map-source sde `
  --map-sde-path D:\eve-sde `
  --map-region 10000045 `
  --map-refresh-on-start
```

Notes:

- `--map-sde-path` should point at the extracted SDE root directory.
- The importer looks for `bsd/mapSolarSystems.yaml`,
  `bsd/mapSolarSystemJumps.yaml`, and, when available,
  `bsd/mapConstellations.yaml`, `bsd/mapRegions.yaml`, and `bsd/invNames.yaml`.
- If BSD map tables are not present, the importer also supports the extracted
  universe layout under `sde/universe/eve/...`, which is common in mirrored SDE
  repositories and compatible with the official YAML package contents.
- If `--map-region` and `--map-system` are both omitted, the importer loads the
  full available topology.

## Tenal Example

`Tenal` is official region `10000045`. A minimal local config looks like this:

```json
{
  "source": "sde",
  "layout_mode": "sde",
  "region_ids": [10000045],
  "sde_path": "D:\\eve-sde"
}
```

## HTTP API

Read the current map config:

```text
GET /api/map/config
```

Update the map config and immediately apply it:

```text
PUT /api/map/config
```

Refresh/import map data for the current source:

```text
POST /api/map/refresh
```

Example SDE refresh payload:

```json
{
  "source": "sde",
  "sde_path": "D:\\eve-sde",
  "region_ids": [10000045]
}
```

Health status now includes a `map` section in `GET /api/health` with:

- active source
- active system/link counts
- config file path
- configured SDE path
- last refresh timestamp
- last refresh error

## Runtime Files

Do not commit these local runtime files:

- `intel_map.json`
- extracted SDE directories
