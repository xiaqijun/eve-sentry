# Repository Guidelines

## Ownership And Scope

This repository is owned by role `30` and contains the standalone public ESI
Gateway: network authentication, caching, rate limiting, health checks, and
metrics. EVE SSO, user tokens, authenticated sessions, and application-specific
resolvers belong in the `eve-sentry` server repository.

Do not copy the complete server ESI package into this repository. Share request
and response behavior through the published contracts instead of importing code
from sibling repositories.

## Multi-Repository Coordination

Route new work through role `00`. Any HTTP, ESI, authentication, or response
shape change must be reviewed by role `05` against the
[multi-repository development workflow](https://github.com/xiaqijun/eve-sentry-contracts/blob/main/docs/development-workflow.md).
Only one task may write to this repository at a time. Role `90` exclusively owns
production deployment, health verification, and rollback.

## Build And Test

Create a virtual environment, install the repository requirements, and run the
full suite with `python -m pytest`. Keep tests deterministic and add regression
coverage for caching, authentication, rate limiting, upstream failures, and
health behavior.

## Commit Guidelines

Use concise Conventional Commit subjects such as `fix: preserve upstream error
budget`. Do not commit local credentials, tokens, virtual environments, caches,
or runtime state.
