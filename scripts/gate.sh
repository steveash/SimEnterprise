#!/usr/bin/env bash
# Canonical quality gate — the SINGLE source of truth run BOTH locally and in CI
# (.github/workflows/ci.yml calls `scripts/gate.sh --check`), so the two can
# never drift. This is the gate that gates a merge.
#
#   scripts/gate.sh            # dev/agent mode: AUTO-FORMAT + lint-fix, then typecheck + test
#   scripts/gate.sh --check    # CI mode: verify only, fail on any unformatted/lint/type/test issue
#
# Run this before `gt done` / opening a PR. In default mode it auto-fixes
# formatting so you cannot accidentally land unformatted code (the #1 cause of
# red CI on main). CI runs the same gate in --check mode.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--check" ]]; then
  echo "== ruff lint ==";         uv run ruff check .
  echo "== ruff format check =="; uv run ruff format --check .
else
  echo "== ruff format (auto) =="; uv run ruff format .
  echo "== ruff lint (--fix) ==";  uv run ruff check --fix .
fi
echo "== mypy (strict) =="; uv run mypy
echo "== pytest ==";        uv run pytest
echo "gate: OK"
