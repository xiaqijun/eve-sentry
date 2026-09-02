# EVE Sentry ESI Gateway

Standalone private proxy for allow-listed, public EVE Online ESI endpoints.

The gateway contains only the public ESI client and network-level concerns:
authentication, caching, rate limiting, and health metrics. EVE SSO,
authenticated sessions, tokens, and application resolvers remain in the
`eve-sentry` service.

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
