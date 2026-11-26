# EVE Sentry ESI Gateway

Transport modes are `legacy`, `dual`, and `relay`. Relay-only forwards authenticated,
allow-listed TLS tunnels to `esi.evetech.net:443`; 114 owns business caches and queues,
verifies the official certificate, and retains ESI tokens. JSON cache routes are disabled;
existing storage is preserved. Use the protected [rollout workflow](../docs/server-deployment.md#受保护的-esi-转发切换).
The cache/envelope behavior below applies to legacy/dual mode, not relay-only.

The server's [personnel archive](../docs/personnel-cache-plan.md) is opt-in.
Public responses now include additive per-entity `freshness` metadata; stale reads
retain the original fetch time. This applies to both the default in-memory cache
and the optional durable ID cache. Affiliation TTL is 3600 seconds in both modes,
matching the current official affiliation endpoint policy, independently of the generic TTL.
Other endpoints retain their existing cache policy. Legacy affiliation TTL settings
are normalized at startup with a warning; existing ID-cache records keep their original
fetch time while expiry is normalized to that time plus 3600 seconds.
Upstream 420/429 responses are returned as 429 with a sanitized `Retry-After`.
Keep `EVE_SENTRY_ESI_GATEWAY_AFFILIATION_CACHE_TTL=3600` in deployment configuration.

Separately deployed private proxy for allow-listed, public EVE Online ESI
endpoints. Its source, CI/CD, and operating documentation live in the
`esi-gateway/` directory of the [EVE Sentry monorepo](../README.md).

The gateway contains only the public ESI client and network-level concerns:
authentication, caching, rate limiting, and health metrics. EVE SSO,
authenticated sessions, tokens, and application resolvers remain in the
root `app/` service.

Documentation:

- [Documentation index](docs/README.md)
- [Cache and storage design](docs/cache-and-storage.md)
- [Operations runbook](docs/operations.md)

For large-scale ESI ID lookups, configure the optional `storage` extra. PostgreSQL
is the long-term source of truth and Redis is a bounded hot tier. Batch responses
are split into one record per requested ID (or normalized name for `/v1/universe/ids`)
before being written to both stores. Expired records remain eligible for stale
serving during the configured grace period while a background refresher retries
them in bounded batches.

## 快速部署

网关和 Sentry 需要位于同一个受信私网，例如 ZeroTier 或 WireGuard。下列参数依次为
网关机器的私网 IPv4、Sentry 服务端发起连接的私网 IPv4，请替换为自己的地址。

**Docker Compose（Linux，容器随宿主机自动重启）**

```bash
bash esi-gateway/deploy/docker/quickstart.sh 10.0.0.10 10.0.0.20
```

**Python 3.10+（Linux 或 Windows，前台运行）**

```text
python3 esi-gateway/scripts/quickstart.py 10.0.0.10 10.0.0.20
```

两条命令均从仓库根目录执行，Windows 可将 `python3` 换成 `py -3`。
首次启动时生成 `esi-gateway/gateway.env`，重复执行会沿用其中的服务令牌。
Relay 模式无需安装 PostgreSQL、Redis 或 Python 第三方依赖。Sentry 侧配置及 systemd 部署见
[快速接入和运维](docs/operations.md#快速接入)。

## Run locally

```powershell
cd esi-gateway
$env:EVE_SENTRY_ESI_GATEWAY_TOKEN = 'use-a-random-secret-at-least-32-bytes'
python scripts/esi_gateway.py --host 127.0.0.1 --port 8787
```

The public gateway routes are:

```text
GET  /health
GET  /v1/characters/{id}
GET  /v1/corporations/{id}
GET  /v1/alliances/{id}
GET  /v1/systems/{id}
POST /v1/universe/names
POST /v1/universe/ids
POST /v1/characters/affiliation
```

All routes except `/health` require the configured bearer token and optional
source-address allow-list. Request bodies are limited to 64 KiB and 1,000
items. The gateway preserves the existing JSON response shape, including the
`data` and `cache` fields.

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

For the complete cache path, PostgreSQL pool behavior, Redis memory guidance,
stale-value semantics, and operational limits, see
[Cache and storage design](docs/cache-and-storage.md).

## CI/CD

Changes pushed to `main` are validated on Python 3.10 through 3.13. Pull requests,
if enabled by the hosting platform, are validation-only and never deploy production.
The workflow runs dependency checks, Ruff, byte-code compilation, the full test
suite, Bash syntax checks, and ShellCheck. Only a validated `main` revision can
enter the protected GitHub `production` environment.
PR and release validation run each documented quick-start command on a fresh Linux runner.
Production packaging waits for both the Docker Compose and Python startup checks.

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
to protect production deployment, health verification, and rollback approval.
Manual workflow runs selected from any branch other than `main` validate only
and cannot deploy. The normal development path is a direct commit and push to
`main`.
