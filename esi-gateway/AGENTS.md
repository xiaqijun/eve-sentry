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
shape change must be reviewed against the server's
[multi-repository development workflow](https://github.com/xiaqijun/eve-sentry/blob/main/docs/multi-repository-development.md)
and API documentation in `eve-sentry/docs/`.
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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **eve-sentry-esi-gateway** (289 symbols, 571 relationships, 19 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/eve-sentry-esi-gateway/context` | Codebase overview, check index freshness |
| `gitnexus://repo/eve-sentry-esi-gateway/clusters` | All functional areas |
| `gitnexus://repo/eve-sentry-esi-gateway/processes` | All execution flows |
| `gitnexus://repo/eve-sentry-esi-gateway/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
