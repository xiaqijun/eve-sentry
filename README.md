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
python -m pytest
```
