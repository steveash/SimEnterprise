#!/usr/bin/env bash
# reconstruct_eval.sh — one-command reproduction of the full attribution eval.
#
# DEPRECATED shim: the attribution eval is now a first-class CLI surface,
# `enterprise-sim reconstruct e2e` (spec 0003), which drives the same chain
# in-process and also writes a machine-readable summary.json. This script now just
# delegates to it so existing muscle memory and docs keep working; it will be
# removed once docs/RECONSTRUCT.md no longer references it.
#
#   scripts/reconstruct_eval.sh --run runs/<id> --backend anthropic_api -o eval/
#   scripts/reconstruct_eval.sh --keyless-smoke -o /tmp/eval   # wiring only, no key
#
# BUILD -> FIDELITY -> oracle/reconstructed/rag reason -> REPORT, every artifact
# under one --out dir. The oracle/reconstructed reason slots call the Claude API
# and need ANTHROPIC_API_KEY + the `bench` extra (`uv sync --extra bench`); that is
# the keyed crew run that fills the AFTER tables. BUILD/FIDELITY/REPORT are keyless.
#
# --keyless-smoke proves the wiring end to end WITHOUT a key: it forces the fake
# backend and substitutes a keyless RAG prediction for all three reason slots. The
# numbers in that mode are wiring stand-ins, NOT an eval result.
#
# All flags (--run, --backend, -o/--out, --model, --limit, --keyless-smoke) map
# 1:1 onto `reconstruct e2e` and are passed through unchanged.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
  grep '^# ' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# Handle --help locally so the shim stays self-describing; everything else is
# forwarded verbatim to the CLI (flags map 1:1).
for arg in "$@"; do
  case "$arg" in
    -h | --help) usage 0 ;;
  esac
done

# `enterprise-sim` is the console script; fall back to `uv run` in a checkout.
ESIM=(enterprise-sim)
command -v enterprise-sim >/dev/null 2>&1 || ESIM=(uv run enterprise-sim)

echo "NOTE: reconstruct_eval.sh is deprecated; use 'enterprise-sim reconstruct e2e'." >&2
exec "${ESIM[@]}" reconstruct e2e "$@"
