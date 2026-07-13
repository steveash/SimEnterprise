#!/usr/bin/env bash
# Canonical quality gate — the SINGLE source of truth run BOTH locally and in CI
# (.github/workflows/ci.yml calls `scripts/gate.sh --check`), so the two can
# never drift. This is the gate that gates a merge.
#
#   scripts/gate.sh            # dev/agent mode: AUTO-FORMAT + lint-fix, then typecheck + test
#   scripts/gate.sh --check    # CI mode: verify only, fail on any unformatted/lint/type/test issue
#
# Run this before committing / opening a PR. In default mode it auto-fixes
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
# Coverage is report-only here (spec 0002 §3): --cov-report= suppresses the per-file
# terminal table (no CI log spam); one summary line follows. COVERAGE_CORE=sysmon uses
# Python 3.12's sys.monitoring core for near-zero overhead. Identical in both modes —
# CI visibility is the point. The `coverage report` call enforces fail_under (slice 6).
echo "== pytest (+coverage) =="; COVERAGE_CORE=sysmon uv run pytest --cov --cov-report=
echo "coverage: $(uv run coverage report --format=total)% total (run 'make coverage' for per-file detail)"
echo "gate: OK"
