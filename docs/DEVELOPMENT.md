# Development guide

Human-oriented setup and dev-loop reference. The agent-oriented equivalent (invariants,
feature workflow) is [`CLAUDE.md`](../CLAUDE.md); both describe the same loop.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) — the only hard requirement; it provisions
  Python 3.12 and the venv itself (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Optional: Node ≥ 22 + npm for `apps/graph-explorer` (its own README covers it).

## Setup and the dev loop

```bash
uv sync --extra dev        # deps for the full keyless gate (make setup)
./scripts/gate.sh          # format + lint-fix + mypy(strict) + pytest   (make gate)
./scripts/gate.sh --check  # what CI runs: verify-only                   (make check)
```

The gate is the contract: **if `./scripts/gate.sh` passes locally, CI passes** — CI
(`.github/workflows/ci.yml`) invokes the same script in `--check` mode, plus a lazy-dep
import smoke (`make smoke`). The suite is ~1000 tests in ~20s, entirely offline and
deterministic (seeded, `fake` LLM backend — decision D31), so run it freely.

While iterating:

```bash
uv run pytest tests/test_scheduler.py            # one file
uv run pytest -k "golden and manifest"           # by keyword
uv run enterprise-sim run examples/golden.toml   # deterministic end-to-end run (make golden)
uv run enterprise-sim eval runs/golden/golden-slice-co-40644d551158
```

`runs/` is gitignored; regenerate anything under it. The golden run's exact shape is
pinned by `tests/test_golden_run.py` and documented in `docs/GOLDEN_RUN.md` — if you
change it deliberately, update both.

## Keyed (real-LLM) development

Everything above needs no key and no network. The live paths — `eval --judge
--backend anthropic_api`, `bench run`, `reconstruct build/reason`, the RAG runner — need:

```bash
uv sync --extra dev --extra bench    # anthropic SDK + claude-agent-sdk (+ query engines)
export ANTHROPIC_API_KEY=...
```

Keyed tests skip automatically when the key is absent; keep it that way for new ones
(reuse `requires_llm_runner` from `tests/test_benchmark_keyless.py`). Any new lazily
imported dependency must be added to `scripts/import_smoke.py`, which CI runs so an
undeclared runtime dep fails the build instead of a live run.

Amazon Bedrock is not yet usable end-to-end — see `specs/0001-bedrock-first-class.md`
for status and the gap list before attempting it.

One-command end-to-end eval (build → fidelity → reason → attribution report):

```bash
scripts/reconstruct_eval.sh --keyless-smoke   # wiring check, no key
scripts/reconstruct_eval.sh --out /tmp/eval   # real run, needs key
```

## Repo orientation

Start with the repo map in [`CLAUDE.md`](../CLAUDE.md). Deeper dives: `PLAN.md`
(decisions D1–D33), `ARCHITECTURE.md` (the § references in docstrings), and the
subsystem docs (`GOLDEN_RUN.md`, `BENCHMARK.md`, `RECONSTRUCT.md`). Feature planning
lives in `specs/` (`specs/README.md` explains the workflow, `specs/ROADMAP.md` the epics).

## Conventions

- Conventional commits (`feat(scope): …`, `fix: …`, `docs: …`), small gate-green slices.
- mypy strict, no blanket `type: ignore`; narrow per-module overrides in `pyproject.toml`.
- Never introduce network, wall-clock time, or unseeded randomness into a default path.
- New business domains/output formats are plugins — never edits to `enterprise_sim/core/`
  (see `skills/author-playbook/`).
