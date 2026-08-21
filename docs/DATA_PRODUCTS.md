# Structured Data Products

> Causal-graph-driven synthetic tables, materialized views, and an iterative
> business-question loop over the simulated enterprise. Extends Enterprise Sim
> beyond documents: the same company that writes design docs and status
> reports now also *produces analytics data*.

```bash
# Keyless demo: all five scenarios, deterministic, no network.
enterprise-sim data run examples/data_products.toml

# The ~100M-row / ~10GB profile (same code path, bigger dial).
enterprise-sim data run examples/data_full_scale.toml

# List registered scenarios.
enterprise-sim data scenarios
```

---

## 1. What a data scenario is

A **data scenario** (`enterprise_sim/data_products/`) declares, as one typed
`ScenarioSpec` (Pydantic, JSON-serializable):

- **Populations** — entity sets (users, opportunities, tickets, accounts,
  campaigns) with **static attributes** sampled once per entity and **panel
  variables** sampled per entity per day.
- **The causal graph** — every variable is either *exogenous* (a distribution:
  normal, lognormal, beta, poisson, bernoulli, categorical, …) or *endogenous*
  (a structural equation: linear predictor over parents with per-level effects
  for categorical parents, transforms, pairwise interactions, a link function,
  noise, clipping; categorical endogenous variables use multinomial-logit
  utilities). Panel equations may reference attributes, global factors,
  same-day panel variables, and **lagged** panel values (habit/persistence).
- **Factors** — global daily latent series (market demand, release quality)
  with trend, weekday and annual seasonality, AR(1) noise, and dated,
  geometrically-decaying **shocks** (a bad release, a price increase, a viral
  moment). Factors are the cross-entity correlation channel.
- **KG bindings** — populations link to the simulated company:
  `kg_identity` binds a population 1:1 to KG nodes (sales reps *are* the
  company's `Person` nodes), and `kg_dimension` draws a categorical
  attribute's levels from KG nodes (each user's `product_line` is a real
  `Project`). All bindings are recorded in `lineage.json`.
- **Tables** — physical parquet tables at `entity` (dimension) or
  `entity_day` (fact) grain, mapping spec variables to columns.
- **Views** — DuckDB SQL over the tables (and earlier views), materialized to
  parquet: the daily/weekly/monthly rollups and joins an analyst would build.
- **Questions** — business questions, each with the DuckDB SQL that answers
  it, grouped into categories. Some deliberately reference data the initial
  spec does not produce — they carry a `gap` fix and drive the loop (§4).

The five built-in scenarios (`data_products/scenarios/`) are **registered
plugins** — `product_growth`, `sales_pipeline`, `support_ops`,
`subscription_finance`, `marketing_attribution` — each a hand-authored causal
graph of a few dozen nodes plus a question bank. Adding a scenario never
touches the engine (the §4 extensibility invariant).

## 2. Determinism & the LLM

Mirrors the document pipeline's contract (ARCHITECTURE.md §7):

- **The deterministic skeleton is the contract.** Template authoring is a pure
  function of `(scenario, window, seed, world)`; every stochastic draw comes
  from a `derive_subseed` sub-stream (numpy generators keyed by
  `(root, purpose, population, …)`), and days are processed in order — so
  chunking, partition sizing, and re-runs never change a sampled value. Same
  seed → **byte-identical parquet** (materialized views are canonicalized
  with `ORDER BY ALL` because DuckDB's parallel aggregation is unordered).
- **The LLM elaborates, never gates.** With `authoring.mode = "llm"` the
  configured backend (through the one `core/llm` client — api/bedrock/cli,
  prompt-cached, cost-ceilinged) authors the spec from the scenario's brief +
  a KG summary via `generate_structured` against the spec's JSON schema,
  proposes extra questions, and drives gap analysis. Every proposal is
  Pydantic-validated **and linted** with bounded repair re-prompts; the
  template is the always-available fallback, so the run completes on any
  backend and stays keyless-green on `fake`.

## 3. Scale

Sampling is vectorized over entities and streamed day by day; each fact
table's rows accumulate into sized parquet parts
(`scale.rows_per_partition`), so memory is bounded by one partition
regardless of total volume. Throughput is roughly ~0.75M rows/s on a laptop
for the product-growth column set; `examples/data_full_scale.toml` (a year ×
48× populations) lands ~100M rows / ~10GB across the five scenarios. The
lint's `MAX_PROJECTED_ROWS` guardrail rejects accidental blowups before any
sampling cost.

## 4. The question loop

Per iteration (up to `loop.iterations`, default 3):

1. **Sample** every table from the current spec; **materialize** the views.
2. **Evaluate** every question: its SQL runs against tables + views; it is
   *answerable* iff it executes, returns rows, and yields non-null values.
3. **Close gaps**: failing questions contribute their `gap` deltas (template
   mode) — additive spec changes: `add_attribute`, `add_panel_variable`,
   `add_kg_dimension`, `add_factor`, `add_table`, `add_table_column`,
   `add_view` — and in llm mode the backend proposes fixes for questions
   without one. Deltas are deduplicated, applied, re-linted, and the loop
   resamples.

The loop exits early when everything is answerable or no fix exists. The
final `questions.json` records, per question: category, text, SQL,
answerability, the iteration it first became answerable, and every attempt's
error — an audit trail of the gap-closing process.

## 5. Output contract

```
runs/data/<scenario>-<digest>/
  manifest.json            # config snapshot, per-iteration stats, LLM cost
  spec.json                # the FINAL causal spec — the ground-truth answer key
  spec.iteration-N.json    # spec as sampled in each loop iteration
  lineage.json             # dim values / identity rows ↔ gold KG node ids
  questions.json           # categories + questions + SQL + answerability history
  data/
    tables/<table>/part-*.parquet   # sized partitions, dictionary-encoded dims
    views/<view>/part-00000.parquet # materialized rollups/joins
```

`spec.json` plays the role the gold KG plays for the corpus: the generating
process itself ships with the data, so causal-discovery, text-to-SQL, and
analytics agents can be scored against known structure.

## 6. Config reference

```toml
scenarios = ["product_growth", ...]   # registered scenario names
seed = 11
output_dir = "runs/data"

[world]                    # exactly one of:
run_dir = "runs/golden/<run-id>"      # link against a completed run's kg/
# — or —
[world.company]                        # inline: Layer A builds it (keyless)
name = "Northwind Tools"
vertical = "software"
size = "medium"

[window]                   # the panel date window
start = 2026-01-01
end = 2026-03-31

[authoring]
mode = "template"          # or "llm"
max_repair_attempts = 2

[scale]
factor = 1.0               # multiplies every population's base size
rows_per_partition = 2000000

[loop]
iterations = 3             # 1-5

[model]                    # used only in llm mode
backend = "fake"           # fake | anthropic_api | bedrock | claude_cli
name = "claude-opus-4-8"
cost_ceiling_usd = 50.0
```

CLI overrides: `-o/--output-dir`, `--scale X`, `--iterations N`.

## 7. Authoring a new scenario

Register a plugin in `data_products/scenarios/` exposing `name`, `title`,
`brief` (the LLM guidance), `build_spec(start, end)` (the deterministic
template — use `scenarios/_helpers.py` for readable equation shorthand), and
`question_bank(spec)` (include a few gap-carrying questions so the loop has
work). `lint_spec` is the authoring feedback loop: run it until clean, then
`tests/test_data_run.py::TestTemplates` picks the scenario up automatically
via the registry.
