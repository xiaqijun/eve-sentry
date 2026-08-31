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

The package deliberately does not include `EveSsoClient`,
`EsiAuthenticatedSession`, `EsiResolver`, authenticated ESI methods, or any
application/server storage code. Those remain owned by `eve-sentry`.

Production deployment is intentionally not performed by this change; the
systemd unit and deployment workflow are release artifacts only.
