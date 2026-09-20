# Enterprise Sim

Generate a fake-but-realistic enterprise organization and all the office-work artifacts it
would produce over a configurable time window — business goals, departments, programs and
scenarios of work, teams, people (with job descriptions, locations, calendars), projects,
and the documents/presentations/schedules those people create, in native formats
(`.docx`, `.pptx`, `.md`, `.json`) — plus the **ground-truth knowledge graph** those
artifacts encode, as a labeled answer key for KG/RAG/search eval.

**Start here:**
- [`PLAN.md`](./PLAN.md) — vision, decisions, and milestones.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — detailed component & plugin design.
- [`docs/GOLDEN_RUN.md`](./docs/GOLDEN_RUN.md) — the v1 end-to-end **golden run** and how its gold KG acts as an answer key.
- [`docs/DATA_PRODUCTS.md`](./docs/DATA_PRODUCTS.md) — **structured data products**: causal-graph-sampled parquet tables, materialized views, and the business-question loop (`enterprise-sim data run`).
- [`docs/EXPLORER.md`](./docs/EXPLORER.md) — the **graph explorer as the one-stop UI**: launch/extend runs with live progress, cost and pause/resume ([`EXPLORER_RUNS.md`](./docs/EXPLORER_RUNS.md)), author department/scenario templates with the `author-playbook` skill ([`EXPLORER_TEMPLATES.md`](./docs/EXPLORER_TEMPLATES.md)), and browse/run/propose eval questions ([`EXPLORER_EVALS.md`](./docs/EXPLORER_EVALS.md)).

## Quickstart

```bash
# Render the v1 golden run: a markdown corpus + gold knowledge graph,
# deterministic and network-free (default `fake` backend).
enterprise-sim run examples/golden.toml
enterprise-sim eval runs/golden/golden-slice-co-40644d551158
```

### Generating with a real model

Every command above is free, network-free and reproducible because `run` defaults
to the deterministic `fake` backend — it renders structurally correct artifacts
whose *prose* is placeholder text. To have a real model write the prose, name a
provider in the config's `[model] backend` and pass `--live`:

```bash
# Unstructured: a quarter of Haiku-authored documents + the gold KG.
# Without --live this silently renders placeholder prose instead.
enterprise-sim run examples/haiku_quarter.toml --live

# Structured: parquet tables + views for the SAME company and quarter,
# linked against the corpus run's KG. (`data run` needs no --live.)
enterprise-sim data run examples/haiku_quarter_data.toml
```

A `--live` run calls a provider, costs money, and is **not** byte-reproducible;
add `--dry-run` to price it first (that estimate counts prompt tokens only, so a
`claude_cli` run — which re-sends the agent's own system prompt per call — costs
more than it shows). `examples/haiku_quarter.toml` uses the
`claude_cli` backend, which routes through a local `claude` OAuth subscription
and needs no `ANTHROPIC_API_KEY`; switch `backend` to `anthropic_api` to use a key.

Status: v1 (markdown-only) end-to-end is wired — world → events → corpus → gold
KG, with a reproducible golden run as the acceptance artifact. Office formats and
new modalities arrive as additive producer plugins (PLAN.md M8–M10).
