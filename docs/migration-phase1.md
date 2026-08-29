# Phase 1 migration design

This repository is the standalone deployment unit for the public ESI Gateway.
It exposes only `/health`, the two `/v1/universe` batch routes, and public
character, corporation, alliance, and system profile routes.

The gateway accepts a bearer service token and optional source-address allow-list,
limits request bodies to 64 KiB and batches to 1,000 items, caches successful
responses in memory, coalesces concurrent misses behind a process lock, and
rate-limits upstream calls. Health output contains only operational counters.

The package deliberately does not include `EveSsoClient`,
`EsiAuthenticatedSession`, `EsiResolver`, authenticated ESI methods, or any
application/server storage code. Those remain owned by `eve-sentry`.

Production deployment is intentionally not performed by this change; the
systemd unit and deployment workflow are release artifacts only.
