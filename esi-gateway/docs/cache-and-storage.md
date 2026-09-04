# Cache and storage design

This document describes the optional PostgreSQL + Redis deployment for the
standalone public ESI Gateway. It is the operational source of truth for cache
behavior; the example environment file contains the corresponding variable
names and defaults.

## Scope

The gateway serves public ESI data only:

| Gateway route | Cache endpoint key | Default TTL |
| --- | --- | ---: |
| `POST /v1/universe/names` | `resolve_names` | 30 days |
| `POST /v1/universe/ids` | `resolve_ids` | 30 days |
| `POST /v1/characters/affiliation` | `get_character_affiliations` | 1 hour |
| `GET /v1/characters/{id}` | `get_character` | 2 days |
| `GET /v1/corporations/{id}` | `get_corporation` | 7 days |
| `GET /v1/alliances/{id}` | `get_alliance` | 7 days |
| `GET /v1/systems/{id}` | `get_system` | 30 days |

TTL values are seconds and can be overridden independently with the
`EVE_SENTRY_ESI_GATEWAY_*_CACHE_TTL` variables. The generic
`EVE_SENTRY_ESI_GATEWAY_CACHE_TTL` remains the fallback for the original
in-memory gateway cache when the durable ID cache is disabled.

This repository does **not** implement authenticated ESI, EVE SSO, user
tokens, or per-account standings/reputation snapshots. Those belong in the
`eve-sentry` server repository. A reputation snapshot there should normally be
cached per authorized account for 5–15 minutes, rather than one request per
contact.

## Read and write path

When PostgreSQL/Redis are configured, `IdCacheCoordinator` uses this order:

1. Read fresh records from Redis.
2. Read missing or expired records from PostgreSQL and warm Redis.
3. Serve stale records inside the stale-grace window and enqueue a refresh.
4. Fetch remaining IDs from public ESI, split the response into one record per
   ID (or normalized name), and write PostgreSQL first and Redis second.

The HTTP response shape is unchanged. Batch requests are reconstructed in the
same order as the caller supplied, while duplicate IDs/names are fetched only
once.

## PostgreSQL durable tier

`PostgresStore` uses `psycopg_pool.ConnectionPool`. Each operation acquires a
connection with `with pool.connection()`, so the connection is returned to the
pool after the query. The current gateway defaults are deliberately conservative
for a 4 vCPU/4 GiB host:

```text
min_size = 1
max_size = 4
```

The pool is opened during startup, creates `esi_id_cache` if necessary, and is
closed during normal gateway shutdown. The schema is also available as
[`deploy/postgres/001_esi_id_cache.sql`](../deploy/postgres/001_esi_id_cache.sql)
for review and provisioning. PostgreSQL remains the durable source of truth;
Redis loss does not delete the long-term cache.

The current gateway pool is intentionally smaller and simpler than the
server repository's separate intel-store pool (which uses 2–8 connections and
a 5-second acquisition timeout). Do not assume the two pools share settings.

## Redis hot tier

Redis stores serialized cache records with an expiry that covers the fresh TTL
plus stale grace. It also provides a per-record distributed refresh lock. A
Redis outage is treated as a degraded hot tier: PostgreSQL and ESI requests
continue where possible, and the `/health` counters record the error.

For the 4C4G/500 GB host profile, configure Redis outside the gateway with an
explicit memory limit and an eviction policy, for example:

```text
maxmemory 128mb  # 128–256 MB is the recommended range
maxmemory-policy allkeys-lru
```

The gateway does not change Redis server policy itself. The Redis URL should
use a dedicated database or key prefix and must not contain credentials in
source-controlled files.

## Refresh, stale values, and retries

- Refresh scheduling runs in one daemon worker.
- The interval is clamped to 5–10 seconds; default is 5 seconds.
- Each scheduler pass selects at most 1,000 queued records.
- Refreshes reuse the gateway's process-wide ESI rate limiter.
- A Redis lock prevents multiple gateway instances from refreshing the same
  record concurrently.
- A failed refresh keeps the previous payload and schedules exponential retry.
- Retry delay starts at 5 seconds and is capped at 300 seconds by default.
- A record is served as stale until `stale_until`; after that it must be
  fetched again or the request returns the existing upstream error shape.

The refresher is demand-driven: records are queued when a request observes a
stale value. It is not a full PostgreSQL table scanner and does not load
millions of IDs into memory. If a complete periodic sweep is required, that
belongs in a separately scheduled server-side job with its own traffic budget.

## Health indicators

`GET /health` retains the existing top-level fields and adds `id_cache` when
the durable coordinator is enabled. Important fields include:

- `hot_hits`, `durable_hits`: cache-layer hit counts;
- `stale_hits`, `refresh_queued`: stale serving and queued work;
- `refresh_batches`, `refresh_success`, `refresh_failures`,
  `refresh_retries`: refresher activity;
- `postgres_errors`, `redis_errors`: backend degradation signals;
- `ttl_seconds`, `ttl_by_endpoint`, `refresh_interval_seconds`,
  `refresh_batch_size`: effective runtime policy.

Alert on sustained backend errors, a growing `pending_refresh`, or a high
stale-hit rate. A high stale-hit rate usually means ESI is unavailable or the
configured TTL is too short for the traffic pattern.
