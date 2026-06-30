# The KG-QA Benchmark — enterprise-agent eval over the gold KG

SimEnterprise is also a **benchmark generator**. The simulator emits the ground
truth most agent-eval setups lack: a **gold knowledge graph**, a **gold answer
key** (provenance — which artifact grounds which fact), and a grounded,
multi-format **artifact corpus** (markdown, Word `.docx`, Jira, email). The
`enterprise_sim.benchmark` package turns that ground truth into a
question/answer benchmark and scores how well an agent answers — with **no human
labelling and no LLM in the generate/score loop**.

- **Package:** [`enterprise_sim/benchmark/`](../enterprise_sim/benchmark/)
- **Epic:** `esim-uzc` · **CLI:** `enterprise-sim bench …`
- **Ground truth:** the [golden run](GOLDEN_RUN.md) (`examples/golden.toml`),
  executed fresh each time so the gold graph and the answer key always agree.

---

## The graph-vs-RAG thesis

The point of the benchmark is a **controlled comparison** of how an agent can
answer the same questions:

- **GRAPH** — query the gold knowledge graph directly (Cypher over `kuzu`,
  SPARQL over the RDF ontology via `pyoxigraph`). Multi-hop, transitive, and
  aggregation questions resolve **exactly**, because the answer *is* a graph
  traversal.
- **RAG** — retrieve over the raw artifact corpus and read off the answer. Strong
  on locally-stated facts, weaker as questions require chaining facts that live
  in different documents.
- **Baseline** — naive heuristics (e.g. answer-nothing, or string match), the
  floor every real runner must clear.

Because every answer is a **set of knowledge-graph node ids**, scoring is exact
and deterministic (set precision/recall/F1) — no LLM judge, no rubric drift. The
benchmark's headline result is the score **broken down by reasoning type**: the
graph runner should pull away from RAG precisely on the multi-hop / transitive /
aggregation rows. That gap is the thesis.

---

## The reasoning-type taxonomy

Every [`QAPair`](../enterprise_sim/benchmark/schema.py) is tagged with the kind
of reasoning it exercises (`REASONING_TYPES`). The generator mints all five from
the gold graph; the scorer reports each as its own row.

| `reasoning_type`  | What it exercises                                   | Example question |
|-------------------|-----------------------------------------------------|------------------|
| `direct_relation` | one edge                                            | *Who does Cleo Costa report to?* |
| `transitive`      | a chain of like edges (skip-levels, dept via team)  | *Who is in Cleo Costa's management chain, all the way up?* |
| `provenance`      | the answer key — which artifacts ground an entity   | *Which artifacts mention or ground Ben Cho?* |
| `aggregation`     | a count/filter over many nodes (answer = the set)   | *How many people are on Quality Engineering?* |
| `goal_tree`       | a goal / sub-goal decomposition walk                | *What advances the goal '…', directly or through its subgoals?* |

For `aggregation`, the gold answer is the **full set being counted** and
`expected_label` is its size, so a count question is graded on whether the agent
identified the right members, not just guessed a number.

---

## Determinism & the keyless guarantee

The generate/score path is **fully deterministic and network-free**:

- The benchmark is generated from a fresh golden run on the default `fake` LLM
  backend (no key, no network, no cost) — the gold graph and answer key are
  produced together, so they can never disagree.
- Every generator walks the graph in sorted order, every answer set is sorted,
  each pair's id is a content hash of its semantics, and the benchmark is sorted
  by a stable key — so the **same gold run yields a byte-identical benchmark**.
- Scoring is pure set math. The same `(benchmark, predictions)` always renders
  the same `Report`.

**Anything that calls an LLM (the graph-agent and RAG runners) MUST skip cleanly
without `ANTHROPIC_API_KEY`**, so the quality gate (`ruff` + `mypy` + `pytest`)
stays green keyless in CI. This is locked by
[`tests/test_benchmark_keyless.py`](../tests/test_benchmark_keyless.py): the
shipped pipeline runs with the key removed, and the shared `requires_llm_runner`
skip marker reports every gated runner test as *skipped* when no key/SDK is
present.

---

## Generate · run · report

All commands are also runnable as `python -m enterprise_sim.cli …`.

### Generate

```bash
# Derive the benchmark from a fresh golden run; write JSONL (default: stdout).
enterprise-sim bench generate -o bench.jsonl

# Or reuse an existing run directory's gold KG.
enterprise-sim bench generate --run runs/golden/<run-id> -o bench.jsonl
```

The benchmark is one [`QAPair`](../enterprise_sim/benchmark/schema.py) per JSONL
line. (The current golden run yields 64 pairs spanning all five reasoning types.)

### Run (a runner produces predictions)

A *runner* answers each question and writes a **predictions JSONL** — one
`{"qa_id": …, "predicted_ids": [...]}` per line
([`Prediction`](../enterprise_sim/benchmark/score.py)). The gated LLM runners
(graph-agent `esim-uzc.4`, RAG baseline `esim-uzc.5`) and the comparison driver
(`esim-uzc.6`) plug in here. See **Adding a runner** below.

### Score / report

```bash
enterprise-sim bench score --bench bench.jsonl --pred runner.preds.jsonl
```

Grades the predictions against the gold benchmark and prints a macro-averaged
report — overall and **per reasoning type**:

```
KG-QA benchmark score
  overall          n=64  F1=0.812  P=0.840  R=0.795  EM=0.640
by reasoning_type:
  aggregation      n=6   F1=0.611  ...
  direct_relation  n=22  F1=0.930  ...
  ...
```

A benchmark pair with no matching prediction is graded against the empty set
(the agent declined to answer); predictions for ids not in the benchmark are
ignored — the benchmark, not the predictions, defines the question set.

> The multi-runner **comparison** report (graph vs RAG vs baseline side by side)
> is `bench report`, landing with `esim-uzc.6`. Until then, `bench score` renders
> the single-runner breakdown.

---

## Architecture

| Module | Role |
|--------|------|
| [`schema.py`](../enterprise_sim/benchmark/schema.py) | `QAPair` + the `Benchmark` collection; JSONL round-trip; `REASONING_TYPES`. |
| [`fixtures.py`](../enterprise_sim/benchmark/fixtures.py) | One deterministic gold KG — executes the golden run (`load_gold_world` / `golden_run`) so generators and tests share one byte-stable source of ground truth. |
| [`generate.py`](../enterprise_sim/benchmark/generate.py) | Deterministically derive `QAPair`s from the gold `World` (and grounding map) across all five reasoning types. Entry point: `generate(run_dir=None)`. |
| [`score.py`](../enterprise_sim/benchmark/score.py) | `Prediction`/`Predictions`, set-based per-item P/R/F1 (`score_item`), macro aggregation (`score` → `Report`), and `format_report` for the CLI. |

Tests: scaffold, generator, scorer, report rendering, and the keyless lock live
in `tests/test_benchmark_*.py`.

---

## Adding a new runner

A runner is anything that, given the benchmark, emits a predictions JSONL the
scorer can grade. The contract is deliberately small:

1. **Read** the benchmark: `Benchmark.read_jsonl("bench.jsonl")`.
2. **Answer** each `QAPair` — resolving the question to a set of KG **node ids**.
   How is up to the runner: Cypher/SPARQL over the graph, retrieval over the
   corpus, a fixed heuristic, etc.
3. **Write** one [`Prediction`](../enterprise_sim/benchmark/score.py) per
   question and serialise with `Predictions.write_jsonl(...)`:

   ```python
   from enterprise_sim.benchmark import Benchmark, Prediction, Predictions

   bench = Benchmark.read_jsonl("bench.jsonl")
   preds = Predictions.of(
       Prediction(qa_id=pair.id, predicted_ids=my_answer(pair))  # tuple/list of node ids
       for pair in bench
   )
   preds.write_jsonl("runner.preds.jsonl")
   ```

4. **Score** it: `enterprise-sim bench score --bench bench.jsonl --pred runner.preds.jsonl`.

**Gating (mandatory for LLM/network runners).** Any runner that needs an API key
or an optional dependency (the agent SDK, `kuzu`, `pyoxigraph`) must skip cleanly
keyless. Reuse the shared marker so the test is *reported as skipped* in keyless
CI:

```python
from tests.test_benchmark_keyless import requires_llm_runner

@requires_llm_runner
def test_graph_agent_runner_answers_direct_relations() -> None:
    ...
```

Keep the heavy/optional imports **inside** the runner (lazy), never at package
import time, so `import enterprise_sim.benchmark` stays keyless (enforced by
`test_pipeline_pulls_in_no_llm_sdk`).

---

## Status

| Bead | Scope | State |
|------|-------|-------|
| `esim-uzc.1` | package scaffold + `QAPair` schema + fixtures | ✅ |
| `esim-uzc.2` | Q/A generator from the gold KG | ✅ |
| `esim-uzc.3` | grader / scorer (`bench score`) | ✅ |
| `esim-uzc.7` | tests + this doc | ✅ |
| `esim-uzc.4` | graph-agent runner (Cypher/SPARQL, gated) | ⏳ |
| `esim-uzc.5` | RAG baseline runner (corpus retrieval, gated) | ⏳ |
| `esim-uzc.6` | comparison report + `bench report` CLI | ⏳ |

The graph/ontology query *engine* and the LLM runners (`esim-uzc.4/.5`) bring in
`kuzu`, `pyoxigraph`, and `claude-agent-sdk`; their tests gate on
`requires_llm_runner` and skip when those are absent, so keyless CI stays green.
