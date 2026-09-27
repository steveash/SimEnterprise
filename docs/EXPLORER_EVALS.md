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
finishes (`world run` finalize step) and on demand by `enterprise-sim evals
generate --world DIR` for runs made from the CLI. Proposed questions are appended to
the same file (the id is a content hash of the question + answer, so duplicates
are rejected).

## 2. Python: `enterprise_sim/evals/` + CLI

Thin wrappers over `enterprise_sim.benchmark` that speak JSON and know the per-run layout:

```
enterprise-sim evals generate  --world DIR [--force] [--critic structural|llm] [--critic-model M]
                                                           # writes evals/questions.jsonl, every pair reviewed by the critic;
                                                           #   --force keeps proposed + human-verified pairs; JSON summary
enterprise-sim evals critique  --world DIR [--critic structural|llm]   # re-review the set in place (verified pairs untouched)
enterprise-sim evals verify    --world DIR --id ID [--expected-ids NODE…] [--label TEXT]
                                                           # a human confirms / corrects one answer: verified, confidence 1.0
enterprise-sim evals list      --world DIR                   # JSON: questions + counts by reasoning_type/qtype/difficulty/source
enterprise-sim evals add       --world DIR --file PROPOSALS.json
                                                           # validates each proposal against kg/nodes.jsonl (every expected id
                                                           #   must exist; reasoning_type in REASONING_TYPES), assigns ids,
                                                           #   appends with source="proposed"; JSON: {added, rejected:[{question, reason}]}
enterprise-sim evals remove    --world DIR --ids ID [ID…]    # only proposed questions may be removed
enterprise-sim evals score     --world DIR --pred PRED.jsonl [--ids …]
                                                           # JSON Report (overall + by reasoning_type + per item), via benchmark.score
enterprise-sim evals sample    --world DIR --fraction F --seed S [--stratify]
                                                           # JSON list of ids: deterministic sample (stratified per reasoning_type when asked)
enterprise-sim evals run       --world DIR --runner rag|graph --out DIR [--ids …] [--fraction F --seed S] [--model M] [--backend B]
                                                           # the Python runners (docs/BENCHMARK.md) as a streaming job:
                                                           #   emits {"kind":"question","id",…} per answer, writes predictions.jsonl + results.json
```

Scoring is shared: `score_item` / `score` from `enterprise_sim/benchmark/score.py` are the
only implementation; the TS port (§3) is tested against fixtures generated from it.

### 2.1 How far each expected answer can be trusted (`enterprise_sim/evals/critic.py`)

Every generated answer is derived mechanically from the gold graph — consistent,
but not always *reasonable*. The **critic** reviews each pair and records, on the
`QAPair` (review metadata, excluded from the id and omitted from the JSONL when
unset): `confidence` (0–1), `critique` (the reasons), `critic`
(`structural` / `llm` / `human`) and `verified`.

- **Structural critic** (keyless, always run): each finding multiplies the
  confidence by a penalty — a subject name shared by several nodes (×0.5, e.g.
  "Billing migration" is both an Initiative and a Project), an answer of the wrong
  kind for the question's opening words (×0.6, a team for "Who…"), a large (>20,
  ×0.75) or huge (>50, ×0.5) answer set, a count label that disagrees with the set
  (×0.4), the same question text with different answers (×0.4); empty answers or
  answers naming unknown nodes are 0.
- **AI critic** (`--critic llm`, `claude_cli` by default so it uses your Claude
  login): batches of pairs, each with its answer and the graph facts around the
  question's subject, get a verdict (`ok` / `suspect` / `wrong`), a confidence and
  a reason. The final confidence is structural × model; `wrong` caps it at 0.2.
- **Human verification** (`evals verify`, or *edit answer…* / *mark verified* in
  the UI) sets `verified`, confidence 1.0, `critic: "human"`; the pair keeps its id
  so past executions still line up. Critics and `--force` regeneration never
  overwrite a verified pair.

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

An **eval execution** (`evalRun` op) takes `{worldPath, selection: {ids} | {fraction, seed, stratify} | 'all', model, concurrency (1–4), runner: 'explorer' | 'rag' | 'graph'}`.
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

### 3.1 Transcripts

Every explorer-runner answer is persisted in full at
`evals/results/<eval-id>/traces/<qa_id>.json` (`EvalTrace`): every thinking block,
text block, tool call with its input and result (up to 4,000 characters), the
submitted ids and label, turns / seconds / cost, and the **error** when the agent
could not run (e.g. it could not authenticate). `evalsReadTrace` serves one;
`evalsReadResult` returns an execution's items with id → label for every
expected/predicted id and the list of traced questions. The rag/graph runners do
not record transcripts.

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

- **Questions** tab — the browser groups by **answer confidence** by default
  (lowest first, so the answers worth a human look come to the top); each row
  carries a confidence meter (green/amber/red, a dashed outline when unreviewed,
  a check when human verified) and the latest F1. *Review expected answers…*
  re-runs the structural or AI critic. The detail pane shows the confidence and the
  critic's notes, an **answer editor** (remove ids, search the graph to add nodes,
  optional label → *Save correction (verified)*, or *looks right — mark verified*),
  the **latest result** — including the error when the run failed, turns / time /
  cost, and the final query — and the **most recent trace** (every step of the agent
  answering it). Run one, a checked selection, a stratified N% sample, or all, with
  the explorer / rag / graph runner, a model (incl. Opus 5.5) and concurrency 1–4.
- **Executions** tab — every past execution; select one for its report and a table
  of every question it answered (F1, EM, turns, time; an *error* badge on failures;
  filter all / failed / misses); select a question for expected vs submitted
  (hits/misses coloured, click-through to Explore), metrics, the error, the final
  query, and its full trace.
- **Propose** tab — unchanged: a chat that proposes questions grounded in queries it
  actually ran; tick the good ones and add them to the set.

## 6. Tests

Python: `evals generate` on the golden run equals `bench generate --world` byte
for byte; `evals add` rejects unknown ids / bad types / duplicates and appends
valid proposals with `source="proposed"`; `evals sample` is deterministic and
stratified; `evals score` matches `bench score`; the QAPair additions round-trip
through JSONL with and without the new fields.
Sidecar: the score port against the Python fixture; sampling parity; the
`submit_answer`/`propose_question` tool wiring and validation (pure, like
`agent.test.ts`); the eval-execution persistence (`eval.json`, `items.jsonl`)
with a stubbed answerer; live agent tests gated on `ANTHROPIC_API_KEY`.
