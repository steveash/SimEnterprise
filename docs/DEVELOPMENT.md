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
uv run enterprise-sim eval runs/golden/golden-slice-co-6c66fbef69f8
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

### Amazon Bedrock

Every real-LLM path also runs against Amazon Bedrock with only AWS credentials — no
`ANTHROPIC_API_KEY` (spec `specs/0001-bedrock-first-class.md`). Requirements:

```bash
uv sync --extra bench           # pulls anthropic[bedrock] (boto3/botocore signing)
export AWS_REGION=us-east-1     # plus your usual AWS creds: keys, AWS_PROFILE, or SSO
```

Region and profile can also be set per run in the `[model]` config block
(`aws_region` / `aws_profile`); when unset, the ambient AWS environment decides.

**Model ids must be inference-profile form.** Bedrock addresses models by dated,
region-scoped inference-profile id (e.g. `us.anthropic.claude-sonnet-4-6-20250929-v1:0`),
not the 1P name (`claude-sonnet-4-6`). Passing a 1P id under `--backend bedrock` **fails
fast** at client build (before any live call, dry-run included) with the exact shape to
set — the build never silently sends an unaddressable id (finding F2).

**Pricing.** The cost ceiling and dry-run estimate (D13) work with Bedrock ids: they are
normalized to their 1P pricing key for lookup. An id that resolves to no pricing row (a
fresh family, or opus behind a custom app-inference-profile ARN) falls back to the default
rate and emits a one-time `warnings.warn` so the degradation is visible, not silent (F5).

Bedrock-enabled entry points:

```bash
uv run enterprise-sim run examples/demo.toml --backend bedrock --model ID   # corpus generation
uv run enterprise-sim eval RUN --judge --backend bedrock --model ID         # LLM-judge
uv run enterprise-sim reconstruct build --backend bedrock --model ID -o DIR # extract/resolve
uv run enterprise-sim reconstruct reason --use-bedrock                      # graph-agent (SDK)
uv run enterprise-sim bench run --runner graph --use-bedrock ...            # graph-agent runner
uv run enterprise-sim bench run --runner rag --backend bedrock --model ID   # RAG runner
```

`--model ID` (a Bedrock inference-profile id) overrides the default/`[model].name`
on every `--backend bedrock` path above — `run`, `eval --judge`, `bench run --runner
rag`, and `reconstruct build` — so the F2 fail-fast names a flag that always exists.

The `--use-bedrock` runners route the `claude-agent-sdk` subprocess to Bedrock via
`CLAUDE_CODE_USE_BEDROCK=1` + `AWS_REGION` (add `--aws-region` to override); the same env
var makes the `claude_cli` backend use Bedrock, which is documented rather than wrapped in
dedicated plumbing.

Validate a live account with the cred-gated smoke (skips cleanly with no AWS creds, so
it is safe to run anywhere; never run by CI):

```bash
uv run python scripts/bedrock_smoke.py   # BEDROCK_SMOKE_MODEL overrides the default id
```

Not yet covered: `reconstruct sweep --models` still defaults to 1P model ids (pass Bedrock
ids explicitly to sweep on Bedrock), and there is no live Bedrock CI job — the smoke is the
manual gate.

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
