#!/usr/bin/env bash
# reconstruct_eval.sh — one-command reproduction of the full attribution eval.
#
# Ties together the six CLI steps the RECONSTRUCT.md "Results" section reports —
# BUILD -> FIDELITY -> oracle/reconstructed/rag reason -> REPORT — so a keyed
# crew run is a single invocation instead of six hand-copied commands with four
# intermediate files to keep in sync. Every artifact lands under one --out dir.
#
#   scripts/reconstruct_eval.sh --run runs/<id> --backend anthropic_api -o eval/
#   scripts/reconstruct_eval.sh --keyless-smoke -o /tmp/eval   # wiring only, no key
#
# The oracle (graph agent on the gold KG) and reconstructed (same agent on the
# reconstructed KG) steps call the Claude API and need ANTHROPIC_API_KEY + the
# `bench` extra (`uv sync --extra bench`); that is the keyed crew run that fills
# the AFTER tables. BUILD/FIDELITY/REPORT are pure and keyless.
#
# --keyless-smoke proves the wiring end to end WITHOUT a key: it forces
# --backend fake and substitutes a keyless RAG prediction for all three
# prediction slots, so REPORT renders over real (if meaningless) inputs. The
# numbers in that mode are wiring stand-ins, NOT an eval result.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN=""
BACKEND="anthropic_api"
OUT="eval"
MODEL="claude-sonnet-4-6"
LIMIT=""
SMOKE=0

usage() {
  grep '^# ' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run) RUN="$2"; shift 2 ;;
    --backend) BACKEND="$2"; shift 2 ;;
    -o | --out) OUT="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --keyless-smoke) SMOKE=1; BACKEND="fake"; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

# `enterprise-sim` is the console script; fall back to `uv run` in a checkout.
ESIM=(enterprise-sim)
command -v enterprise-sim >/dev/null 2>&1 || ESIM=(uv run enterprise-sim)

mkdir -p "$OUT"
LIMIT_ARGS=()
[[ -n "$LIMIT" ]] && LIMIT_ARGS=(--limit "$LIMIT")

say() { printf '\n=== %s ===\n' "$*" >&2; }

# 0. A golden run to reconstruct (fresh, deterministic fake sim) if none given.
if [[ -z "$RUN" ]]; then
  say "golden run (fresh)"
  "${ESIM[@]}" run examples/golden.toml -o "$OUT/runs"
  RUN=$(find "$OUT/runs" -mindepth 1 -maxdepth 1 -type d | head -1)
fi
echo "run: $RUN" >&2

# A shared benchmark generated from that run's gold graph.
say "bench generate"
"${ESIM[@]}" bench generate --run "$RUN" -o "$OUT/bench.jsonl"

# 1. BUILD — reconstruct + persist the KG once.
say "build (backend=$BACKEND)"
"${ESIM[@]}" reconstruct build --run "$RUN" -o "$OUT/recon" --backend "$BACKEND"

# 2. FIDELITY — score the reconstruction against the gold graph (keyless).
say "fidelity"
"${ESIM[@]}" reconstruct fidelity --reconstructed "$OUT/recon" --run "$RUN" \
  --json -o "$OUT/fidelity.json"

# 3. REASON — three prediction files: oracle / reconstructed / rag.
if [[ "$SMOKE" -eq 1 ]]; then
  # Keyless wiring smoke: one keyless RAG prediction stands in for all three
  # slots so REPORT runs without a key. NOT a real eval — proves the plumbing.
  say "reason (keyless smoke: rag stands in for all three slots)"
  "${ESIM[@]}" bench run --runner rag --backend fake --run "$RUN" \
    --bench "$OUT/bench.jsonl" -o "$OUT/pred.rag.jsonl"
  cp "$OUT/pred.rag.jsonl" "$OUT/pred.oracle.jsonl"
  cp "$OUT/pred.rag.jsonl" "$OUT/pred.reconstructed.jsonl"
  echo "NOTE: --keyless-smoke numbers are wiring stand-ins, not an eval." >&2
else
  say "reason: oracle (graph agent on gold KG)"
  "${ESIM[@]}" bench run --runner graph --run "$RUN" --bench "$OUT/bench.jsonl" \
    -o "$OUT/pred.oracle.jsonl" --model "$MODEL" "${LIMIT_ARGS[@]}"
  say "reason: reconstructed (same agent on reconstructed KG)"
  "${ESIM[@]}" reconstruct reason --reconstructed "$OUT/recon" \
    --bench "$OUT/bench.jsonl" -o "$OUT/pred.reconstructed.jsonl" \
    --model "$MODEL" "${LIMIT_ARGS[@]}"
  say "reason: rag (corpus baseline)"
  "${ESIM[@]}" bench run --runner rag --backend "$BACKEND" --run "$RUN" \
    --bench "$OUT/bench.jsonl" -o "$OUT/pred.rag.jsonl"
fi

# 4. REPORT — attribute the graph's advantage (understanding vs reasoning).
say "report"
"${ESIM[@]}" reconstruct report --bench "$OUT/bench.jsonl" \
  --oracle "$OUT/pred.oracle.jsonl" \
  --reconstructed "$OUT/pred.reconstructed.jsonl" \
  --rag "$OUT/pred.rag.jsonl" \
  --fidelity "$OUT/fidelity.json" -o "$OUT/attribution.md"

echo >&2
echo "wrote: $OUT/attribution.md  (+ fidelity.json, recon/, pred.*.jsonl)" >&2
