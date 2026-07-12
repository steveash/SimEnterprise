#!/bin/bash
# SessionStart hook: make a fresh Claude Code web session immediately able to run
# the quality gate (./scripts/gate.sh) without a manual setup step.
set -euo pipefail

# Local checkouts manage their own venv; only bootstrap remote (web) sessions.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
uv sync --extra dev
