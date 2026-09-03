# EVE Sentry ESI Gateway

Standalone private proxy for allow-listed, public EVE Online ESI endpoints.

The gateway contains only the public ESI client and network-level concerns:
authentication, caching, rate limiting, and health metrics. EVE SSO,
authenticated sessions, tokens, and application resolvers remain in the
`eve-sentry` service.

For large-scale ESI ID lookups, configure the optional `storage` extra. PostgreSQL
is the long-term source of truth and Redis is a bounded hot tier. Batch responses
are split into one record per requested ID (or normalized name for `/v1/universe/ids`)
before being written to both stores. Expired records remain eligible for stale
serving during the configured grace period while a background refresher retries
them in bounded batches.

## Run locally

```powershell
$env:EVE_SENTRY_ESI_GATEWAY_TOKEN = 'use-a-random-secret-at-least-32-bytes'
python scripts/esi_gateway.py --host 127.0.0.1 --port 8787
```

## Test

```powershell
python -m pip install '.[test]'
python -m ruff check .
python -m pytest
```

Install production storage drivers with `python -m pip install '.[storage]'`.
Set `EVE_SENTRY_ESI_GATEWAY_POSTGRES_DSN` and
`EVE_SENTRY_ESI_GATEWAY_REDIS_URL` in the service environment to enable the
two-tier ID cache. The refresher is intentionally bounded for a 4 vCPU/4 GiB
host: it runs every 5–10 seconds and processes at most 1,000 IDs per batch.
The default cache policy follows the low-speed refresh design: ID/name mappings
(`resolve_names` and `resolve_ids`) use a 30-day TTL, character profiles use
2 days, current character affiliations use 1 hour, corporation/alliance
profiles use 7 days, and universe systems use 30 days. Override these with the
`*_CACHE_TTL` environment variables in the deployment example. Expired records
remain available for the configured stale grace period (5 minutes by default).
Retries use exponential backoff up to `EVE_SENTRY_ESI_GATEWAY_CACHE_RETRY_MAX`.
The `/health` response keeps its existing fields and adds an `id_cache` object
with hot-hit, refresh, retry, and backend-error counters.

## CI/CD

Pull requests and changes to `main` are validated on Python 3.10 through 3.13.
The workflow runs dependency checks, Ruff, byte-code compilation, the full test
suite, Bash syntax checks, and ShellCheck. Only a validated `main` revision can
enter the protected GitHub `production` environment.

Deployment artifacts are deterministic tar archives with a SHA-256 checksum and
an embedded revision manifest. The remote deployer stores immutable releases in
`/opt/eve-sentry-esi-gateway/releases`, atomically switches the `current`
symlink, restarts the systemd service, and verifies `/health`. A failed restart
or health check restores the previous unit and release automatically.

Configure these GitHub production environment values:

- Variables: `EVE_SENTRY_ESI_GATEWAY_DEPLOY_HOST`,
  `EVE_SENTRY_ESI_GATEWAY_DEPLOY_USER`, and
  `EVE_SENTRY_ESI_GATEWAY_DEPLOY_PORT`.
- Secrets: `EVE_SENTRY_ESI_GATEWAY_SSH_KEY` and
  `EVE_SENTRY_ESI_GATEWAY_KNOWN_HOSTS`.
- Optional variables: `EVE_SENTRY_ESI_GATEWAY_ROOT`,
  `EVE_SENTRY_ESI_GATEWAY_SERVICE_NAME`,
  `EVE_SENTRY_ESI_GATEWAY_HEALTH_URL`, and
  `EVE_SENTRY_ESI_GATEWAY_KEEP_RELEASES`.

The target host must already contain the service user, the environment file at
`/etc/eve-sentry-esi/gateway.env`, and an executable Python virtual environment
at `/opt/eve-sentry-esi-gateway/.venv/bin/python`. The SSH deployment account
must run the remote script as root so it can manage the configured systemd unit
and gateway root. Configure required reviewers on the `production` environment
so role `90` retains ownership of production deployment, health verification,
and rollback approval. Manual workflow runs selected from any branch other than
`main` validate only and cannot deploy.
