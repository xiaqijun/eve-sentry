# Repository Guidelines

## Project Structure & Module Organization
`app/` contains the application code. UI components live in `app/ui/`, OCR and capture logic in `app/engine/`, persistence models in `app/models/`, and the intel web server in `app/server/`. Tests are in `tests/` and follow the runtime modules they cover. Static assets such as `resources/alert.wav` live in `resources/`. Supporting scripts belong in `scripts/`, and design notes or plans are kept in `docs/`.

## Build, Test, and Development Commands
Create an environment and install dependencies with `python -m venv .venv` then `.\.venv\Scripts\pip install -r requirements.txt`.

Run the desktop app with `python main.py`.

Run the intel map server with `python -m app.server --host 127.0.0.1 --port 8765`.

Run the full test suite with `pytest`. For focused work, use `pytest tests/test_detector.py` or similar. Regenerate the alert sound asset with `python scripts/generate_alert.py`.

## Coding Style & Naming Conventions
Use 4-space indentation, type hints where practical, and short module docstrings like the existing files. Keep modules snake_case, classes PascalCase, functions and variables snake_case, and prefer explicit imports from `app.*`. Follow the current style of small, single-purpose classes and straightforward control flow over heavy abstraction.

## Testing Guidelines
This project uses `pytest` with `tests/test_*.py` naming. Add unit tests alongside the affected module and cover both happy-path and regression cases, especially around OCR parsing, threat cooldowns, and file-backed state. Keep tests deterministic by avoiding real disk writes when a stub or in-memory path will do.

## Commit & Pull Request Guidelines
Recent history uses Conventional Commit prefixes such as `feat:`, `fix:`, and `chore:`. Keep commit subjects imperative and concise, for example `fix: handle missing capture source`. Pull requests should describe the user-visible change, note test coverage, and link any related issue or design note. Include screenshots or short recordings for UI changes in `app/ui/`.

## Security & Configuration Tips
Do not commit local runtime data such as `whitelist.json`, virtual environments, or generated caches. PaddleOCR-related environment setup is handled in `main.py`; preserve that behavior when changing startup code so offline or restricted-network setups keep working.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **eve-sentry** (2853 symbols, 6087 relationships, 211 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

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
