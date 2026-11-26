# Operations runbook

## 快速接入

在网关主机克隆本仓库，按 [快速部署](../README.md#快速部署) 选择 Docker Compose 或 Python。
两种方式均以 Relay 模式启动，监听指定的私网地址和端口 `8787`，只接受所填的 Sentry 来源地址。
首次启动在仓库中生成 `esi-gateway/gateway.env`，使用本机文件中的服务令牌完成接入。

首次使用网关的 Sentry 服务端设置以下环境变量，地址换成网关输出的地址。

```ini
EVE_SENTRY_SERVER_ENABLE_ESI=1
EVE_SENTRY_ESI_TRANSPORT=relay
EVE_SENTRY_SERVER_ESI_GATEWAY_URL=http://10.0.0.10:8787
EVE_SENTRY_SERVER_ESI_GATEWAY_TOKEN=网关文件中的服务令牌
```

启动 Sentry 后，在「系统管理 → ESI 网关观测」查看连接。Docker 方式再次运行同一命令可重建容器，
服务令牌保留。停止 Docker 网关时从仓库根目录运行
`docker compose -f esi-gateway/deploy/docker/compose.yaml down`。Python 方式按 Ctrl+C 停止。
Linux 原生常驻部署可使用下文的 systemd 单元。

## Prerequisites

For the systemd installation, provide:

- Python 3.10 or newer and a virtual environment at
  `/opt/eve-sentry-esi-gateway/.venv`;
- PostgreSQL and Redis reachable when the optional legacy/dual ID cache is configured;
- the service environment file at
  `/etc/eve-sentry-esi/gateway.env`;
- the `eve-sentry-esi-gateway` systemd unit and a service account;
- outbound HTTPS access to `esi.evetech.net`.

Install storage drivers when using the optional ID cache:

```bash
/opt/eve-sentry-esi-gateway/.venv/bin/pip install '.[storage]'
```

When PostgreSQL storage is configured, the gateway creates its table during startup. Operators can inspect
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

### Personnel freshness recovery

A healthy HTTP response is not sufficient to verify personnel classification.
After deploying a freshness fix through the protected workflow, use an authorized
affiliation lookup for a currently observed character and verify that
`freshness[character_id].fetched_at` is positive. Repeat the same lookup: a cache
hit must retain the original timestamp, not the second request time. Verify this
even when the optional PostgreSQL/Redis ID cache is disabled. Also verify that
affiliation `expires_at - fetched_at` is 3600 seconds and that an expired
response can be refreshed. A positive but hours-old timestamp is not enough.

Deploy the gateway TTL fix before the server scheduling fix using their protected
workflows. Set `EVE_SENTRY_ESI_GATEWAY_AFFILIATION_CACHE_TTL=3600`; other legacy
values are normalized with a startup warning, including when ID caching is disabled.
Keep server personnel profiles whose affiliation timestamp is zero or over an hour old.
The server repairs distant deadlines at every activity tier, even existing same-priority
jobs, without stealing leases or canceling failure backoff. All expired affiliations
remain eligible for the continuously running priority queues; activity changes order,
not cache validity. Successful jobs schedule their next expiry without a new sighting.
If a still-old gateway
response arrives during rollout, retry is scheduled after 60 seconds, not tomorrow.
Normal active-priority refresh should replace the unknown/old timestamp, reclassify valid
current observations and emit `alert.updated` when the roster changes. Confirm
the resulting bot delivery separately; cleared observations must not return.
Do not clear the personnel database, stamp old profiles with the current time,
or disable the untrusted-affiliation guard. Visual presence/clear must not wait
for personnel refresh. A rollback to missing metadata or day-long affiliation
caching can make confirmation unavailable again; `/health` alone will not reveal it.

### Protected release process

The workflow in [`.github/workflows/deploy-esi-gateway.yml`](../../.github/workflows/deploy-esi-gateway.yml)
validates Python 3.10–3.13, dependency consistency, Ruff, byte-code
compilation, tests, Bash syntax, and ShellCheck. Only a push to `main` creates
an artifact and can deploy to the protected `production` environment.

Deployment creates a deterministic archive, verifies its SHA-256 checksum,
uploads it over SSH, installs an immutable release, switches the `current`
symlink atomically, restarts systemd, and checks `/health`. A failed restart or
health check restores the previous release. Production deployment,
verification, and operator-triggered rollback must use the protected workflow
and the documented operating procedures.

Never commit the service environment file, PostgreSQL password, Redis password,
SSH keys, bearer tokens, or generated archives. Use GitHub environment secrets
and the host-local environment file instead.
