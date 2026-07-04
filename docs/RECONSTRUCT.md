# Reconstruct → Reason — reading the corpus back into a KG

The simulator runs *forward*: it projects a gold **knowledge graph** into a
grounded, multi-format **artifact corpus** (markdown, Word, Jira, email). The
`enterprise_sim.reconstruct` package runs that arrow *backward* — it reads the raw
corpus back out into a **reconstructed knowledge graph**, measures how faithfully
it recovered the gold graph, then reasons over the reconstruction to answer the
same KG-QA benchmark the gold graph answers.

- **Package:** [`enterprise_sim/reconstruct/`](../enterprise_sim/reconstruct/)
- **Epic:** `esim-nc6` · **CLI:** `enterprise-sim reconstruct …`
- **Ground truth:** the [golden run](GOLDEN_RUN.md); the same
  [KG-QA benchmark](BENCHMARK.md) grades every system here.

---

## The inverse-of-generation thesis

Most "build a KG from documents" pipelines have no answer key: you extract a
graph, but nothing tells you which entities and edges you *should* have found, so
you can't separate "the extractor is bad" from "the documents didn't say." This
project does have one. Because the corpus was **generated from** a gold graph, we
know the exact graph the corpus encodes. That makes reconstruction a *measurable*
inverse of generation:

```
        generate (forward)                     reconstruct (inverse)
 gold KG ───────────────────▶ corpus ───────────────────▶ reconstructed KG
              │                                  │
              └───────────── the same gold KG is the answer key ──────────┘
```

So we can score the reconstructed KG against the gold KG **exactly and
keylessly** (node/edge precision/recall/F1, entity-resolution errors, and
**provenance grounding** — whether the reconstruction recovers which artifacts
ground each entity), and — the payoff — decompose an agent's end-to-end error
into *understanding* the corpus vs. *reasoning* over what it understood.

---

## The pipeline

`reconstruct build` runs four stages end to end (`run_pipeline`), turning the raw
corpus into a persisted KG in the **exact gold on-disk schema** so the benchmark's
graph engines load it unchanged:

| Stage | Bead | What it does |
|-------|------|--------------|
| **chunk** | nc6.2 | Hierarchically carve each corpus artifact (markdown, Jira) into `Chunk`s. |
| **extract** | nc6.3 | Read each chunk into typed `MentionSpan`s + candidate `(src, rel, dst)` triples, schema-guided by the ontology. *Gated LLM step.* |
| **resolve** | nc6.4 | Cluster surface-form mentions into canonical typed `Node`s (entity resolution / canonicalization). *Gated LLM step.* |
| **aggregate** | nc6.5 | Rewrite every candidate triple over canonical ids, dedupe to one edge per `(src, rel, dst)` with a support count + provenance, and gate on an aggregated confidence. |

The ontology (`enterprise_sim.reconstruct.ontology`) fixes the target vocabulary —
node types like `Person`, `Team`, `Department`, `Project`, `Goal`, `Initiative`,
and relations like `reports_to`, `member_of`, `has_department`, `advances_goal`,
`subgoal_of` — so extraction and scoring share one schema.

Only **extract** and **resolve** call an LLM. With the deterministic `fake`
backend the whole pipeline still emits a small, loadable KG with **no key**, so
`build` / `fidelity` / `report` are all exercised in keyless CI.

### Build-once, answer-many

Reconstruction is expensive (LLM extraction + resolution over the whole corpus);
reasoning is per-question. So the KG is **built once and persisted**
(`nodes.jsonl` / `edges.jsonl` / `provenance.jsonl`), and every downstream
step — fidelity scoring, and *every* benchmark question the reasoner answers —
reuses that single artifact. The reasoner loads it **once** into the embedded
Cypher (kuzu) + SPARQL (oxigraph) engines and answers the whole benchmark over
that one set of engines; nothing is rebuilt per query. The persisted node
provenance is projected into derived `mentions` edges at load time (as the gold
KG's is), so the provenance reasoning family — "which artifacts ground X" — is
answerable over the reconstruction rather than a structural zero.

The same principle powers the **threshold sweep** (`extract_once` →
`PipelineExtraction`): chunk/extract/resolve run **once**, and only the final
`aggregate` stage re-runs at each edge-confidence threshold
(`PipelineExtraction.build`). Extracting once and re-thresholding many is what
makes the sweep affordable — no LLM call per threshold.

### Tuning the edge-confidence threshold

The `aggregate` stage gates each deduped edge on its aggregated confidence (the
greatest confidence among the chunks that attest it). `--edge-threshold 0.0` (the
default) keeps **every** resolvable edge — maximal recall, but low-confidence
wrong edges tank precision (edge F1 ≈ 0.219 on the golden run). Raising the bar
drops the weakest edges: precision climbs, recall falls, and the F1 sweet spot
sits in between.

`reconstruct sweep` finds it. It extracts the corpus once and re-aggregates that
single extraction at each `--thresholds` value, scoring every rebuilt KG against
the gold graph with the keyless fidelity scorer:

```bash
enterprise-sim reconstruct sweep --run runs/<run-id> \
    --thresholds 0,0.25,0.5,0.75 --backend anthropic_api \
    -o sweep.md                      # '--json' for a machine-readable curve
```

The output is a threshold → node/edge P/R/F1 table (node metrics are invariant —
the threshold gates edges only) plus a callout of the best edge F1. Because
raising the threshold only *removes* edges, edge recall and the kept-edge count
are monotonically non-increasing across the sweep while precision trends up toward
the sweet spot. With `--backend fake` the whole sweep runs keyless.

### Comparing extraction models

Extraction quality is the ceiling on fidelity: a stronger model reads more
entities and relations correctly out of the corpus, so the same corpus
reconstructed by Haiku vs Sonnet yields different graphs. `reconstruct sweep
--models` sweeps that orthogonal axis. Unlike the threshold sweep it can't reuse
one extraction — each model runs its own `chunk → extract → resolve` — so it
reconstructs the corpus once per model and compares:

```bash
enterprise-sim reconstruct sweep --run runs/<run-id> \
    --models claude-haiku-4-5-20251001,claude-sonnet-4-6 \
    --backend anthropic_api \
    --edge-threshold 0.5 \            # single build threshold for every model
    -o model-sweep.md                # '--json' for a machine-readable table
```

The output is a per-model node/edge P/R/F1 comparison table with a callout of the
edge-F1 leader. Add `--bench bench.jsonl` and each model's KG is also reasoned
over — by that *same* model, via the graph agent — and graded, adding an
**answer-F1** column and an answer-F1 leader callout (the agent step needs
`ANTHROPIC_API_KEY`). With `--backend fake` the iteration and reporting run
keyless: the fake backend invents meaningless entities that never match the gold
graph, so every model scores the same degenerate fidelity — the model label is
just recorded, and real per-model numbers are a keyed crew run.

---

## Running it

The full workflow is four commands. `--run` points every step at one golden run so
the gold graph, the reconstruction, and the benchmark all agree; omit it and each
step spins a fresh golden run.

```bash
# 0. A shared benchmark + a golden run to reconstruct.
enterprise-sim run examples/golden.toml -o runs/            # -> runs/<run-id>
enterprise-sim bench generate --run runs/<run-id> -o bench.jsonl

# 1. BUILD — reconstruct + persist the KG once (keyless with --backend fake).
enterprise-sim reconstruct build --run runs/<run-id> -o recon/ \
    --backend anthropic_api          # 'fake' for a keyless smoke build

# 2. FIDELITY — how faithful is the reconstruction? (keyless, no LLM)
enterprise-sim reconstruct fidelity --reconstructed recon/ --run runs/<run-id> \
    --json -o fidelity.json          # drop --json for a markdown report

# 3. REASON — answer the benchmark three ways (each writes predictions JSONL).
#    ORACLE = graph agent on the GOLD KG (the ceiling):
enterprise-sim bench run --runner graph --run runs/<run-id> --bench bench.jsonl \
    -o pred.oracle.jsonl
#    RECONSTRUCTED = the SAME agent on the reconstructed KG (build-once):
enterprise-sim reconstruct reason --reconstructed recon/ --bench bench.jsonl \
    -o pred.reconstructed.jsonl
#    RAG = the corpus-retrieval baseline:
enterprise-sim bench run --runner rag --run runs/<run-id> --bench bench.jsonl \
    -o pred.rag.jsonl

# 4. REPORT — attribute the graph's advantage (keyless, no LLM).
enterprise-sim reconstruct report --bench bench.jsonl \
    --oracle pred.oracle.jsonl \
    --reconstructed pred.reconstructed.jsonl \
    --rag pred.rag.jsonl \
    --fidelity fidelity.json -o attribution.md
```

Steps 1 (real backend) and 3 (all three reasoners) need a model/key **and the
`bench` extra** — `--backend anthropic_api`, `reconstruct reason`, and `bench run
--runner graph` import the `anthropic` / `claude-agent-sdk` runtime deps that only
`--extra bench` installs (`uv sync --extra bench`; `--extra dev` is keyless-only).
Steps 2 and 4 are pure and deterministic and run on `--extra dev` alone.

---

## Scale — beyond the single golden run

Steps 1–4 evaluate one company. A single run can't tell "the reconstructor
generalizes" from "it happens to fit this one company", so `reconstruct scale`
runs the eval across **several varied runs** and aggregates the fidelity:

```bash
# Generate N deterministic, varied gold runs (engineering vs retail, size bands),
# reconstruct + score each, and aggregate node/edge P/R/F1 as mean ± spread.
# Keyless with --backend fake; a real keyed aggregation is --backend anthropic_api.
enterprise-sim reconstruct scale --runs 2 -o aggregate.md
```

Each run's gold graph is the deterministic `fake` sim (so the answer key is
reproducible no matter the reconstruction backend); only the reconstruction's LLM
steps use `--backend`. The report has a per-run table (archetype, size, node/edge
F1, sizes) and an aggregate table (mean, population stdev, min, max) over every
metric — so the headline is a *distribution* across companies, not one number.
`--json` emits the same aggregate machine-readably.

---

## Reading the attribution report

The report puts three systems on **one** benchmark:

- **oracle** — the graph agent on the **gold** KG. The ceiling: a perfect graph
  *and* the graph reasoner. What's achievable when understanding the corpus is free.
- **reconstructed** — the **same** graph agent on the **reconstructed** KG. Same
  reasoner, imperfect graph.
- **rag** — the retrieval baseline: no graph, read the answer off the corpus.

Because oracle and reconstructed share the reasoner and differ *only* in graph
quality, the oracle's advantage over RAG splits cleanly into two additive gaps:

```
 (oracle − rag)   =   (oracle − reconstructed)   +   (reconstructed − rag)
 total graph adv.        understanding gap             reasoning gap
```

| Gap | Formula | What it measures |
|-----|---------|------------------|
| **understanding** | oracle − reconstructed | The cost of imperfectly *understanding* the corpus — reconstruction error — with the reasoner held constant. This is the loss the fidelity numbers explain. |
| **reasoning** | reconstructed − rag | What the graph *structure* still buys over plain retrieval, even reconstructed imperfectly. |
| **total** | oracle − rag | The full advantage of the graph ceiling over RAG. |

The report shows overall **and per-reasoning-type** F1 for all three systems, then
the signed gaps per reasoning type — so you can see *where* each kind of error
lives. A large **understanding** gap on `transitive` rows says the reconstruction
dropped the edges those chains walk (look at edge fidelity + under-merges); a large
**reasoning** gap on `aggregation`/`goal_tree` rows says the graph structure is
what makes those answerable at all, and RAG can't chain the facts. The
reconstruction's fidelity numbers (node/edge F1, over/under-merge counts) ride
along at the top as the context that *explains* the understanding gap.

---

## Results — before / after the gap fixes

The first full-64 attribution run (epic `esim-ecr`) pinpointed *where* the
reference app loses: it is bottlenecked by **understanding** (reconstruction),
not reasoning, and reconstruction failed *totally* on two families — **Goals**
and **provenance** — both scoring fidelity F1 **0.000**. Those two gaps became
[`esim-ecr.1`](../enterprise_sim/reconstruct/) (Goal recovery) and `esim-ecr.2`
(provenance grounding edges). The tables below record the **BEFORE** (the run
that motivated the fixes) next to an **AFTER** placeholder that a keyed crew run
fills once the fixes are in.

The numbers are **not keyless**: the oracle, reconstructed, and rag reasoners all
call the Claude API, so producing them is a keyed crew run (`ANTHROPIC_API_KEY` +
`uv sync --extra bench`). The harness and this doc are the keyless deliverable;
the AFTER column is filled by that crew run.

### Reproducing it — one command

`scripts/reconstruct_eval.sh` runs the whole chain end to end — the six commands
from [Running it](#running-it) (`build → fidelity → oracle/reconstructed/rag →
report`) with every artifact landing under one `--out` dir:

```bash
# Keyed crew run — fills the AFTER tables (needs a key + the bench extra):
uv sync --extra bench
scripts/reconstruct_eval.sh --run runs/<run-id> --backend anthropic_api -o eval/
#   -> eval/attribution.md  (+ fidelity.json, recon/, pred.{oracle,reconstructed,rag}.jsonl)
# Omit --run to spin a fresh golden run; --limit N caps the reasoners for a cheap probe.

# Keyless wiring smoke — proves the plumbing with NO key (fake backend; a keyless
# RAG prediction stands in for all three reasoners, so the numbers are wiring
# stand-ins, not an eval). This is what CI exercises:
scripts/reconstruct_eval.sh --keyless-smoke -o /tmp/eval
```

### Attribution — overall F1 (higher is better)

| System | BEFORE F1 | AFTER F1 |
|--------|-----------|----------|
| **oracle** (graph agent on gold KG — the ceiling) | 0.984 | 0.984 |
| **reconstructed** (same agent on reconstructed KG) | 0.240 | **0.289** |
| **rag** (corpus-retrieval baseline) | 0.223 | 0.223 |

| Gap (oracle advantage split) | BEFORE | AFTER |
|------------------------------|--------|-------|
| **understanding** (oracle − reconstructed) | +0.744 | **+0.695** |
| **reasoning** (reconstructed − rag) | +0.017 | **+0.066** |
| **total** (oracle − rag) | +0.761 | +0.761 |

_AFTER measured on the golden run, Haiku, all 64 questions (oracle + rag reused
— gold KG and corpus are unchanged). The understanding gap shrank (+0.744 →
+0.695) and the reasoning gap ~4×'d (+0.017 → +0.066): `transitive` reasoning
nearly doubled (0.216 → 0.412) as recovered structure let the agent walk more
chains. `goal_tree` and `provenance` **answer**-F1 remain 0.000 — the Goal/
provenance **nodes** are recovered (see fidelity below) but the `subgoal_of` /
`advances_goal` edges and mention→artifact grounding those questions traverse
are not yet sufficiently reconstructed; that edge/grounding recovery is next._

BEFORE reading: the graph ceiling is near-perfect (0.984) but the *reconstructed*
graph barely clears RAG — almost the entire graph advantage (+0.761) is the
**understanding** gap (+0.744). Reasoning over a graph is nearly free once you
have one; *recovering* the graph from prose is where the loss lives. AFTER should
show the understanding gap shrinking as reconstruction fidelity climbs, with
oracle held ~constant (the gold KG and reasoner are unchanged).

### Reconstruction fidelity — the context that explains the understanding gap

| Metric | BEFORE | AFTER | Fixed by |
|--------|--------|-------|----------|
| node F1 | 0.506 | **0.564** (precision 0.778 → **1.000**) | — |
| edge F1 | 0.219 | **0.256** | `esim-ecr.3` threshold sweep (edge-threshold was 0.00 → low precision) |
| **Goal** fidelity F1 | **0.000** | **1.000** (all 3 recovered) | **`esim-ecr.1`** (Goal recovery) |
| **provenance** fidelity F1 | **0.000** | **0.400** | **`esim-ecr.2`** (provenance grounding edges) |
| edge-confidence threshold | 0.00 | _sweep-tuned_ | `esim-ecr.3` |

Per-reasoning-type BEFORE, the graph decisively beat RAG on structured/multi-hop
questions — `aggregation` +0.66, `direct_relation` +0.29, `transitive` +0.18 —
while `provenance` was the one family RAG *won* (graph fidelity 0.000, so the
reconstructed graph had nothing to ground with). `esim-ecr.2` makes that family
answerable over the reconstruction; the AFTER run confirms whether it flips.

> **Filling AFTER:** run the keyed command above against a fresh run, read
> `eval/attribution.md` (overall + gap tables) and `eval/fidelity.json` (node/edge
> and per-type F1, including `provenance.overall.f1` and the `Goal` rows), and
> paste the numbers into the `_TBD_` cells.
