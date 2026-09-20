# Explorer: the eval runner

> Part of [`EXPLORER.md`](./EXPLORER.md). The **Evals** view is where a run's
> evaluation question set lives: explore it (search, grouped by reasoning type /
> question type / difficulty / source), run one question, a sampled %, or the
> whole set with a live accuracy score, keep the history of eval executions, and
> **propose new questions with an AI chat** that grounds every proposal in the
> run's gold graph before you add it to the set.

---

## 1. What the questions are

A run's eval questions are the **KG-QA benchmark** pairs
([`docs/BENCHMARK.md`](./BENCHMARK.md)): one
[`QAPair`](../enterprise_sim/benchmark/schema.py) per question — `question`,
`reasoning_type` (`direct_relation` · `transitive` · `provenance` · `aggregation`
· `goal_tree`), `qtype` (who / count / which / what), `difficulty`, and the gold
answer as a **set of KG node ids** (`expected_ids`, with `expected_label` for
counts). They are derived deterministically from the run's gold KG, so every
answer is exact and scoring is pure set math (precision / recall / F1 / exact
match) — no judge, no rubric drift.

Two additive fields on `QAPair` (defaults keep existing JSONL readable):
`source: str = "generated"` (`"generated"` | `"proposed"`) and
`tags: tuple[str, ...] = ()` (free-form, e.g. the proposal chat's theme).

**Per-run storage**: `<run>/evals/questions.jsonl`. It is written when a job
finishes (`job run` finalize step) and on demand by `enterprise-sim evals
generate --run DIR` for runs made from the CLI. Proposed questions are appended to
the same file (the id is a content hash of the question + answer, so duplicates
are rejected).

## 2. Python: `enterprise_sim/evals/` + CLI

Thin wrappers over `enterprise_sim.benchmark` that speak JSON and know the per-run layout:

```
enterprise-sim evals generate  --run DIR [--force]         # writes evals/questions.jsonl; JSON summary (counts by type)
enterprise-sim evals list      --run DIR                   # JSON: questions + counts by reasoning_type/qtype/difficulty/source
enterprise-sim evals add       --run DIR --file PROPOSALS.json
                                                           # validates each proposal against kg/nodes.jsonl (every expected id
                                                           #   must exist; reasoning_type in REASONING_TYPES), assigns ids,
                                                           #   appends with source="proposed"; JSON: {added, rejected:[{question, reason}]}
enterprise-sim evals remove    --run DIR --ids ID [ID…]    # only proposed questions may be removed
enterprise-sim evals score     --run DIR --pred PRED.jsonl [--ids …]
                                                           # JSON Report (overall + by reasoning_type + per item), via benchmark.score
enterprise-sim evals sample    --run DIR --fraction F --seed S [--stratify]
                                                           # JSON list of ids: deterministic sample (stratified per reasoning_type when asked)
enterprise-sim evals run       --run DIR --runner rag|graph --out DIR [--ids …] [--fraction F --seed S] [--model M] [--backend B]
                                                           # the Python runners (docs/BENCHMARK.md) as a streaming job:
                                                           #   emits {"kind":"question","id",…} per answer, writes predictions.jsonl + results.json
```

Scoring is shared: `score_item` / `score` from `enterprise_sim/benchmark/score.py` are the
only implementation; the TS port (§3) is tested against fixtures generated from it.

## 3. Sidecar: the explorer answerer (`src/sidecar/evals/`)

The primary runner is **in-process**: the explorer already holds the run's
Kùzu + Oxigraph engines and an Agent SDK harness with graph tools, exactly the
setup of the Python graph-agent runner (`benchmark/runners/graph_agent.py`). One
question = one agent turn with:

- the Explore tool set (`graph_schema`, `search_nodes`, `cypher_query`,
  `sparql_query`, `neighbors`, `provenance`, …) **plus** `submit_answer({node_ids:
  string[], label?: string})` — the agent must call it exactly once; the turn's
  prediction is that set (empty if it never submits within `maxTurns`);
- a system prompt built from the benchmark's reference queries per reasoning type
  (`benchmark/runners/reference.py`, ported) and the strict instruction to answer
  with ids only;
- no `highlight` side effects during batch runs (the `emitViz` hook is a no-op),
  but a **single-question run** does highlight the predicted vs expected sets on
  the graph (green = correct, red = wrong, amber = missed) and switches to Explore
  if the user asks.

An **eval execution** (`evalRun` op) takes `{runPath, selection: {ids} | {fraction, seed, stratify} | 'all', model, concurrency (1–4), runner: 'explorer' | 'rag' | 'graph'}`.
`'explorer'` runs in the sidecar; `'rag'`/`'graph'` spawn `enterprise-sim evals run`
through the Python bridge (same event stream shape). Either way the sidecar writes:

```
<run>/evals/results/<eval-id>/          # eval-id = <yyyymmdd-hhmmss>-<runner>-<n>q
  eval.json         # {"eval_id", "runner", "model", "selection", "question_ids", "started_at", "finished_at",
                    #  "status": running|done|cancelled|failed, "usage": {...}, "cost_usd", "report": Report JSON}
  predictions.jsonl # {"qa_id","predicted_ids"} per line — the exact format `enterprise-sim bench score` grades
  items.jsonl       # per question: predicted, expected, P/R/F1/EM, turns, final query, seconds, error?
```

Streamed events (`watchEval` / stream id): `question_started {id}`,
`question_done {id, predicted_ids, expected_ids, precision, recall, f1, exact, engine?, query?, seconds, cost_usd?}`,
`progress {done, total, macro_f1_so_far, by_reasoning_type}`, `done {report}`,
`error`. Cancel = `cancelEval`; finished questions are kept and the report is
computed over them (`status: cancelled`).

Scoring in TS (`src/sidecar/evals/score.ts`) is a direct port of
`score_item`/macro aggregation, unit-tested against a fixture produced by the
Python function so the two cannot disagree. Sampling (`sample.ts`) is a seeded
Fisher–Yates over sorted ids, stratified per reasoning type when asked, and is
tested to match `enterprise-sim evals sample` for the same seed (both use the
same algorithm over the same sorted input; the Python side is the reference).

## 4. Proposing questions with AI (`src/sidecar/evals/propose.ts`)

A chat whose purpose is to turn "I want questions like X" into grounded
`QAPair`s:

- Agent SDK turn with the graph tools **plus** `propose_question({question,
  reasoning_type, expected_ids, expected_label?, difficulty, rationale, tags})`.
  The prompt instructs the agent to *derive every answer from the graph with a
  query* (never from memory), to propose 3–8 varied questions per turn, and to
  favour the reasoning types the user asked for. The sidecar validates each call
  as it arrives (ids exist in the loaded model; type is valid; not a duplicate of
  an existing question) and emits `{kind:'proposal', proposal, valid, reason?}`.
- The panel accumulates proposals in a review list with the answer resolved to
  labels (names) and a *show on graph* button; the user ticks the ones to keep and
  clicks **Add to eval set** → `evalsAdd` → `enterprise-sim evals add` (the Python
  side re-validates and assigns ids) → the browser refreshes with `source: proposed`.
- Multi-turn: "more like the second one but about goals" resumes the session.

## 5. Renderer: the Evals view

- **Question browser** (left): search box (fuzzy over question text + expected
  labels), group-by selector (reasoning type · question type · difficulty ·
  source · tag), counts per group, and per-question row: text, type badges, last
  score (from the most recent execution that included it), *Run* button.
- **Run controls** (top): *Run selected* (checked rows), *Run N %* (slider,
  stratified toggle, seed), *Run all*; runner + model + concurrency; live
  progress bar and macro-F1 so far; *Cancel*.
- **Question detail** (right): question, expected answer (ids resolved to labels,
  clickable → Explore), latest prediction with per-item P/R/F1/EM, the agent's
  final engine + query, and the turn's tool trace.
- **Executions** tab: history of `evals/results/*` with report tables (overall +
  by reasoning type, the same shape as `bench score`), open/compare, export the
  predictions path (for `bench report`).
- **Propose** tab: the proposal chat + review list described in §4.

## 6. Tests

Python: `evals generate` on the golden run equals `bench generate --run` byte
for byte; `evals add` rejects unknown ids / bad types / duplicates and appends
valid proposals with `source="proposed"`; `evals sample` is deterministic and
stratified; `evals score` matches `bench score`; the QAPair additions round-trip
through JSONL with and without the new fields.
Sidecar: the score port against the Python fixture; sampling parity; the
`submit_answer`/`propose_question` tool wiring and validation (pure, like
`agent.test.ts`); the eval-execution persistence (`eval.json`, `items.jsonl`)
with a stubbed answerer; live agent tests gated on `ANTHROPIC_API_KEY`.
