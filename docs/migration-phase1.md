# Phase 1 migration design

This repository is the standalone deployment unit for the public ESI Gateway.
It exposes only `/health`, the two `/v1/universe` batch routes, and public
character, corporation, alliance, and system profile routes.

The gateway accepts a bearer service token and optional source-address allow-list,
limits request bodies to 64 KiB and batches to 1,000 items, caches successful
responses in memory, coalesces concurrent misses behind a process lock, and
rate-limits upstream calls. Successful responses are cached for the configured TTL (default
24 hours), with a bounded 4,096-entry LRU and per-key single-flight so concurrent misses
do not create duplicate ESI calls. Health output includes cache evictions, inflight requests,
and coalesced misses.
Upstream failures are negatively cached for 30 seconds by default. When an expired
successful value is available within the 300-second stale grace window, the gateway
serves it as `cache: "stale"` during an ESI outage. Health output exposes negative
cache hits/entries and stale responses.

The optional large-scale ID cache adds PostgreSQL as the durable store and Redis
as the hot store. `/v1/universe/names` and `/v1/universe/ids` responses are split
into per-ID/per-normalized-name records before persistence. A daemon refresher
runs every 5–10 seconds, caps each batch at 1,000 IDs, uses Redis refresh locks,
retries failures with exponential backoff, and retains stale values until the
stale grace period ends. Health metrics expose cache-layer hits, refresh batches,
retry counts, and PostgreSQL/Redis errors.

The package deliberately does not include `EveSsoClient`,
`EsiAuthenticatedSession`, `EsiResolver`, authenticated ESI methods, or any
application/server storage code. Those remain owned by `eve-sentry`.

Production releases are built and validated from `main`, then pass through the
protected GitHub `production` environment. The deployment uses an immutable
release directory and atomic `current` symlink. The previous systemd unit and
release are restored automatically when restart or health verification fails.
Production approval, verification, and any operator-initiated rollback remain
owned by role `90`.
