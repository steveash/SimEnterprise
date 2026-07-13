# CLAUDE.md — Enterprise Sim

Enterprise Sim generates a fake-but-realistic enterprise organization and the office-work
artifacts it would produce (documents, review threads, schedules — markdown/JSON today,
`.docx`/`.pptx` later), **plus the ground-truth knowledge graph those artifacts encode**.
The output is a labeled corpus + answer key for evaluating KG construction, RAG, and
agentic search systems. On top of generation sit two eval harnesses: `bench` (KG-QA over
the gold graph) and `reconstruct` (rebuild the KG from the corpus, score fidelity).

Toolchain: Python 3.12 · uv · pydantic v2 · ruff · mypy (strict) · pytest (decision D33).

## The one rule

Before you finish any change — before committing or opening a PR — run:

```bash
./scripts/gate.sh
```

It auto-formats (`ruff format`), applies lint fixes, then runs mypy (strict) and the full
pytest suite (~1000 tests, ~20s, fully keyless/offline). CI runs the **same script** in
`--check` mode, so if the gate passes locally, CI passes. Do not hand-run a subset of the
checks; run the script. Historically, skipping the format step was the #1 cause of red CI.

## Dev loop

```bash
uv sync --extra dev                 # one-time setup (also run by the SessionStart hook)
./scripts/gate.sh                   # format + lint + mypy + pytest  (make gate)
uv run pytest tests/test_run.py -k name   # a single test file / test while iterating
uv run enterprise-sim run examples/golden.toml            # deterministic end-to-end run
uv run enterprise-sim eval runs/golden/golden-slice-co-6c66fbef69f8   # structural metrics
uv sync --extra dev --extra bench && uv run python scripts/import_smoke.py  # real-LLM dep smoke
```

`make help` lists shortcuts for all of the above. Everything above is deterministic and
network-free: the default LLM backend is `fake` (D31), so same seed → same run.

## Repo map

| Path | What it is |
|---|---|
| `enterprise_sim/core/` | Engine: KG store (`world/`), event log, clock/scheduler/resolver (`sim/`), config+seed, plugin registries, LLM layer (`llm/`) |
| `enterprise_sim/authoring/` | Declarative playbook/process SDK + lint + test kit + evaluators (§12–13) |
| `enterprise_sim/archetypes/`, `playbooks/`, `processes/`, `producers/` | The four plugin registries' content — new business domains and output formats live here |
| `enterprise_sim/world_builders/` | Layer A: company → goals → departments → teams → people → projects |
| `enterprise_sim/assembly/` | Run orchestration, manifest, corpus layout, validation |
| `enterprise_sim/exporters/` | Gold-KG export (JSONL canonical, Neo4j) |
| `enterprise_sim/benchmark/` | KG-QA benchmark: generate/run/score/report (docs/BENCHMARK.md) |
| `enterprise_sim/reconstruct/` | Corpus → reconstructed KG → fidelity/attribution (docs/RECONSTRUCT.md) |
| `enterprise_sim/cli.py` | `enterprise-sim {run,lint,eval,bench,reconstruct}` |
| `tests/` | Keyless by default; keyed tests skip without `ANTHROPIC_API_KEY` |
| `scripts/` | `gate.sh` (quality gate), `import_smoke.py`, `reconstruct_eval.sh` (one-command e2e eval) |
| `specs/` | Feature workflow: `ROADMAP.md` (epics) + one spec per feature — see `specs/README.md` |
| `apps/graph-explorer/` | Electron app for exploring a run's gold KG (own npm toolchain, own README) |
| `skills/author-playbook/` | Agent skill: author a new business domain as a validated playbook |

Design docs: `PLAN.md` (vision, milestones M1–M10, decisions D1–D33 — the review log that
constrains changes) and `ARCHITECTURE.md` (component design; § references throughout the
code point here). Subsystem docs: `docs/GOLDEN_RUN.md`, `docs/BENCHMARK.md`,
`docs/RECONSTRUCT.md`. Human setup guide: `docs/DEVELOPMENT.md`.

## Invariants — do not break these

- **Never edit `enterprise_sim/core/**` to add a business domain or output format.**
  New domains are authored plugins (`skills/author-playbook`); new formats are producer
  plugins. If a plugin seems to need a new engine primitive, you are probably missing an
  existing pattern (the extensibility invariant, D2/D24).
- **Events are the contract** (D2): the engine emits format-agnostic business events;
  core never imports a format library (`python-pptx`, docx, …) — only producers do.
- **Determinism** (D10/D31): the test suite and default runs are seeded, offline, and
  reproducible. Never add network calls, wall-clock time, or unseeded randomness to a
  default path. Real-LLM SDKs are imported lazily inside methods, never at module top.
- **Keyed code is gated**: anything requiring `ANTHROPIC_API_KEY`/AWS creds must be
  skipped keyless (see `requires_llm_runner` in `tests/test_benchmark_keyless.py`) and
  its lazy deps covered in `scripts/import_smoke.py` so CI catches undeclared imports.
- **mypy strict is non-negotiable**: no untyped defs, no blanket `type: ignore`. New
  optional deps without stubs get a narrow override in `pyproject.toml` like the existing
  `claude_agent_sdk` one.
- **Renders come from a timestamped `WorldView` projection** — producers can only
  reference entities that exist at that sim time; don't bypass this.
- **Reproducibility artifacts**: a run dir must stay self-describing (`manifest.json`,
  config snapshot). `tests/test_golden_run.py` pins the golden run's exact shape — if a
  deliberate change shifts it, regenerate and update the pin *and* `docs/GOLDEN_RUN.md`.

## Feature workflow

1. Epic-level direction lives in **`specs/ROADMAP.md`**. Pick the top unclaimed item (or
   the one the user asks for).
2. Write/extend a spec in `specs/NNNN-slug.md` from `specs/TEMPLATE.md` — capture the why,
   the design sketch, and acceptance criteria before writing code. Small fixes don't need
   a spec; anything multi-session does.
3. Implement in gate-green slices: every commit passes `./scripts/gate.sh`. Prefer several
   reviewable commits over one megacommit; use conventional-commit style
   (`feat(scope): …`, `fix: …`, `docs: …`) as in the existing history.
4. Update the spec's status header and any affected docs (`docs/*.md`, this file) in the
   same PR. Acceptance = the spec's criteria demonstrably hold (tests or a reproducible
   command, like the golden run).

## Model policy (cost routing)

Committed agent definitions in `.claude/agents/` pin each stage of the workflow to the
cheapest model that does the job well; use them instead of doing everything in the main
session's model. Agent prompts are deltas — custom subagents load this file automatically,
so their definitions state only the role-specific rules, never restate these invariants.

| Stage | Agent | Model |
|---|---|---|
| Epic decomposition, spec authoring, hard design calls | `spec-architect` | Fable (high effort) |
| Implementing an approved spec slice | `implementer` | Opus |
| Routine diff review (docs, tests, plugin-level code) | `quick-reviewer` | Sonnet |
| Adversarial review of high-stakes diffs | `adversary` | Fable (high effort) |

Adversarial review is trigger-based, not periodic. Route a diff to `adversary` (and skip
`quick-reviewer`) when it touches: `enterprise_sim/core/**`, benchmark/fidelity scoring or
gold-KG export, the golden-run pin, determinism (seeding/time/randomness/concurrency), the
keyless-gate dependency boundary — or when a slice grew well beyond its spec. Everything
else gets the Sonnet review; the deterministic gate does the rest. Bulk
exploration/search belongs in cheap subagents (Explore/haiku), never in a Fable context.

## Keyed vs keyless

Two dependency extras: `dev` (everything for the keyless gate, including embedded
kuzu/pyoxigraph query engines) and `bench` (the real-LLM runtime: `anthropic` SDK +
`claude-agent-sdk`). The keyless gate must never require `bench`, a key, or the network.
Live paths (`--backend anthropic_api`, `bench run`, `reconstruct build/reason`) need
`--extra bench` plus `ANTHROPIC_API_KEY`. Amazon Bedrock is wired end-to-end (the
`bedrock` backend, `run --backend bedrock`, the `--use-bedrock` runners) and needs
`--extra bench` plus AWS creds instead of a 1P key; model ids must be inference-profile
form (a 1P id fails fast at build) — see `specs/0001-bedrock-first-class.md`.
