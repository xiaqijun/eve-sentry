# Repository Guidelines

## Monorepo Ownership

This repository is the only source repository for the warning server, Windows
client, standalone ESI Gateway, bot, download site, web console, and published
wire contracts. Role `20` owns server paths (`app/`, `frontend/`, and server
deployment assets); component-specific code stays under `client/`, `bot/`,
`esi-gateway/`, and `download-site/`. Do not recreate or update the retired
standalone component repositories.

Route new work through role `00`. Any HTTP, SSE, event, cursor, authentication,
or ESI wire change must be reviewed against the
[monorepo development workflow](docs/multi-repository-development.md)
and the API documentation in `docs/`.
Only one task may write to this repository at a time. Role `90` exclusively owns
production deployment, health verification, and rollback.

## Project Structure & Module Organization
`app/` contains the server and shared server-side domain code; the HTTP service lives in `app/server/`. The Windows client, including UI, capture/OCR, assets, packaging, and tests, lives in `client/`. The bot, ESI Gateway, download site, and web console live in `bot/`, `esi-gateway/`, `download-site/`, and `frontend/`. Root `tests/` cover the server; component tests stay under their component directory. Supporting server scripts belong in `scripts/`, and design notes or plans are kept in `docs/`.

## Build, Test, and Development Commands
Create a server environment and install dependencies with `python -m venv .venv` then `.\.venv\Scripts\pip install -r requirements-server.txt`.

Run the desktop app from `client/` with `python main.py` after installing `client/requirements-onnx.txt`.

Run the intel map server with `python -m app.server --host 127.0.0.1 --port 8765`.

Run server tests with `pytest tests`. Run client tests from `client/` with `pytest tests`. Regenerate the alert sound asset from `client/` with `python scripts/generate_alert.py`.

## Coding Style & Naming Conventions
Use 4-space indentation, type hints where practical, and short module docstrings like the existing files. Keep modules snake_case, classes PascalCase, functions and variables snake_case, and prefer explicit imports from the current component's `app.*` package. Follow the current style of small, single-purpose classes and straightforward control flow over heavy abstraction.

## Testing Guidelines
This project uses `pytest` with `tests/test_*.py` naming. Add server tests under `tests/` and client tests under `client/tests/`; cover both happy-path and regression cases, especially around OCR parsing, threat cooldowns, and file-backed state. Keep tests deterministic by avoiding real disk writes when a stub or in-memory path will do.

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit prefixes such as `feat:`, `fix:`, and `chore:`. Keep commit subjects imperative and concise, for example `fix: handle missing capture source`. Pull requests should describe the user-visible change, note test coverage, and link any related issue or design note. Include screenshots or short recordings for UI changes in `client/app/ui/`.

## Security & Configuration Tips
Do not commit local runtime data such as `whitelist.json`, virtual environments, or generated caches. Client OCR environment setup is handled in `client/main.py`; preserve that behavior when changing startup code so offline or restricted-network setups keep working.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **eve-sentry** (10719 symbols, 25216 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
| `gitnexus://repo/eve-sentry/context` | Codebase overview, check index freshness |
| `gitnexus://repo/eve-sentry/clusters` | All functional areas |
| `gitnexus://repo/eve-sentry/processes` | All execution flows |
| `gitnexus://repo/eve-sentry/process/{name}` | Step-by-step execution trace |

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
