# Contributor & Agent Guide — SimEnterprise

## The quality gate — run this before you finish

**Before committing / running `gt done` / opening a PR, run:**

```bash
./scripts/gate.sh
```

This is the **single source of truth** for the quality gate. In default mode it
**auto-formats** the code (`ruff format`), applies lint fixes, then runs mypy
(strict) and pytest. CI (`.github/workflows/ci.yml`) runs the *same script* in
`--check` mode (`./scripts/gate.sh --check`) — verify-only, no auto-fix.

Because CI and the local gate are one script, they cannot drift. If
`./scripts/gate.sh` passes locally, CI passes.

### Why this matters

The historical #1 cause of red CI on `main` was **unformatted code**: agents ran
`ruff check` / `mypy` / `pytest` but not `ruff format --check`, so formatting
drift passed local checks and only failed in CI *after* merge. Running
`./scripts/gate.sh` (which auto-formats) makes that impossible — do not skip it.

Do **not** hand-format or run only a subset of checks. Run the script.

## Toolchain

uv + ruff (lint + format) + mypy (strict) + pytest, Python 3.12 (decision D33).
Dependencies: `uv sync --extra dev`.

## Task tracking

This repo uses **bd (beads)**; run `bd prime`. See the gastown workspace
`CLAUDE.md` for the full agent workflow. Prefix: `esim`.
