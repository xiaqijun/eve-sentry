# Operations runbook

## Prerequisites

The production host should provide:

- Python 3.10 or newer and a virtual environment at
  `/opt/eve-sentry-esi-gateway/.venv`;
- PostgreSQL reachable using the configured DSN;
- Redis reachable using the configured URL;
- the service environment file at
  `/etc/eve-sentry-esi/gateway.env`;
- the `eve-sentry-esi-gateway` systemd unit and a service account;
- outbound HTTPS access to `esi.evetech.net`.

Install the storage drivers in the production virtual environment:

```bash
/opt/eve-sentry-esi-gateway/.venv/bin/pip install '.[storage]'
```

The gateway creates its PostgreSQL table during startup. Operators can inspect
or pre-apply the equivalent migration in
[`deploy/postgres/001_esi_id_cache.sql`](../deploy/postgres/001_esi_id_cache.sql).

## Recommended 4C4G configuration

Start with the checked-in example and set real secrets locally:

```ini
EVE_SENTRY_ESI_GATEWAY_REFRESH_INTERVAL=5
EVE_SENTRY_ESI_GATEWAY_REFRESH_BATCH_SIZE=1000
EVE_SENTRY_ESI_GATEWAY_ID_CACHE_TTL=2592000
EVE_SENTRY_ESI_GATEWAY_CHARACTER_CACHE_TTL=172800
EVE_SENTRY_ESI_GATEWAY_AFFILIATION_CACHE_TTL=3600
EVE_SENTRY_ESI_GATEWAY_CORPORATION_CACHE_TTL=604800
EVE_SENTRY_ESI_GATEWAY_ALLIANCE_CACHE_TTL=604800
EVE_SENTRY_ESI_GATEWAY_SYSTEM_CACHE_TTL=2592000
EVE_SENTRY_ESI_GATEWAY_CACHE_RETRY_BASE=5
EVE_SENTRY_ESI_GATEWAY_CACHE_RETRY_MAX=300
```

`EVE_SENTRY_ESI_GATEWAY_RATE` is a single gateway-wide upstream limiter shared
by foreground requests and background refreshes. The current default is
2 requests/second; reduce it to `1` or lower if the host's traffic budget
requires more headroom. Separate foreground/background limiters are not
currently implemented in this gateway.

## Startup and health verification

Run locally or under systemd with:

```bash
/opt/eve-sentry-esi-gateway/.venv/bin/python \
  /opt/eve-sentry-esi-gateway/current/scripts/esi_gateway.py
```

Verify the service and health endpoint:

```bash
systemctl status eve-sentry-esi-gateway --no-pager
curl --fail http://127.0.0.1:8787/health
journalctl -u eve-sentry-esi-gateway -n 100 --no-pager
```

The exact unit name and health URL may be overridden by the deployment
workflow variables. The health endpoint is intentionally unauthenticated so
the deployment verifier can use it; keep it bound to the private network.

## CI/CD and rollback

The workflow in [`.github/workflows/deploy-esi-gateway.yml`](../.github/workflows/deploy-esi-gateway.yml)
validates Python 3.10–3.13, dependency consistency, Ruff, byte-code
compilation, tests, Bash syntax, and ShellCheck. Only a push to `main` creates
an artifact and can deploy to the protected `production` environment.

Deployment creates a deterministic archive, verifies its SHA-256 checksum,
uploads it over SSH, installs an immutable release, switches the `current`
symlink atomically, restarts systemd, and checks `/health`. A failed restart or
health check restores the previous release. Production deployment,
verification, and operator-triggered rollback remain owned by role `90`.

Never commit the service environment file, PostgreSQL password, Redis password,
SSH keys, bearer tokens, or generated archives. Use GitHub environment secrets
and the host-local environment file instead.
