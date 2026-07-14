# 0003 — End-to-end eval hardening: `reconstruct e2e`, score baselines, matrix, keyed CI

Status: done (keyed live validation pending AWS/1P creds — the `golden-keyed`
baseline is unseeded until the first owner-dispatched `Keyed eval` run)
Epic: ROADMAP E3
Owner: unclaimed

## Why

The eval loop (golden run → bench → reconstruct → attribution report) is the product's
proof of value, but today it is held together by a shell script and hand-copied numbers:

- The one-command harness is `scripts/reconstruct_eval.sh` (a bash wrapper over six CLI
  invocations, `reconstruct_eval.sh:59-109`), not a CLI surface. Its `--keyless-smoke`
  mode exists (`reconstruct_eval.sh:43,81-90`) but **nothing runs it in CI**:
  `.github/workflows/ci.yml` (39 lines total) runs only `gate.sh --check` and
  `import_smoke.py`. `docs/RECONSTRUCT.md:301` claims "This is what CI exercises" — that
  claim is stale/false today.
- Regression tracking is hand-recorded markdown: the BEFORE/AFTER tables in
  `docs/RECONSTRUCT.md:304-373` and the round-2 tables at `:429-488` are pasted numbers
  with a "paste the numbers into the `_TBD_` cells" instruction (`:372-373`). Nothing
  fails when a change silently moves fidelity — even though the fake-backend fidelity
  numbers are fully deterministic (D10/D31) and *meaningful*, because the deterministic
  structural extractor carries real recall (`member_of` 0.89, `part_of` 1.00 on the
  golden run, `docs/RECONSTRUCT.md:66-74`).
- Every fidelity headline is measured on one tiny golden run. `reconstruct scale`
  (esim-ecr.5, `enterprise_sim/reconstruct/scale.py`) already generates varied runs
  (archetype × size catalog, seed = `seed + index`, `scale.py:127-160`) but has no
  explicit seed axis, no committed baseline, and no CI wiring.
- Keyed eval runs are ad-hoc crew runs; there is no workflow that runs them with repo
  secrets and publishes the leaderboard.

E1 delivered Bedrock end-to-end (`--backend bedrock`, `--use-bedrock` on
`bench run`/`reconstruct reason` — `cli.py:547-557,1482-1492`; spec 0001), and E2
delivered cassette record/replay (`tests/llm_stubs.py`; spec 0002). E3 builds the
harness on top of both.

Constraints: D10/D31 (deterministic, offline default paths), D13 (cost ceiling on keyed
paths), D33 (toolchain), D2 (no core edits needed — everything here is
benchmark/reconstruct/assembly-layer and CI), CLAUDE.md invariants (keyless gate never
requires `bench`/key/network; golden pin; run dirs self-describing). The keyless gate's
~20s feedback loop must not degrade materially.

## Audit: what already exists (do not re-derive)

- `reconstruct_eval.sh` orchestrates: fresh golden run when `--run` omitted → `bench
  generate` → `reconstruct build` → `reconstruct fidelity --json` → three reason slots
  (oracle = `bench run --runner graph`, reconstructed = `reconstruct reason`, rag =
  `bench run --runner rag`) → `reconstruct report`. `--keyless-smoke` forces
  `--backend fake` and copies one keyless RAG prediction into all three slots
  (`reconstruct_eval.sh:81-90`) — wiring proof, numbers are stand-ins.
- All the underlying steps are already library functions the CLI wraps:
  `benchmark.generate.generate` (`generate.py:391`), `reconstruct.build.run_pipeline`
  (`build.py:341`), `reconstruct.fidelity.score_fidelity` (`fidelity.py:557`),
  `benchmark.runners.rag.run_rag` (`rag.py:493`), `reconstruct.attribution
  .build_attribution` (`attribution.py:201`). FIDELITY and REPORT are pure/keyless.
- `reconstruct` is already an argparse command group (`cli.py:794-830`) with
  build/fidelity/sweep/reason/report/scale; `eval` is a **flat** command with a
  positional `run` argument (`cli.py:1848-1867`) documented in CLAUDE.md's dev loop.
- `reconstruct scale`: `default_run_specs(count, seed=7)` draws from a 6-entry
  archetype×size catalog, spec seeds are `seed + index` (`scale.py:137-160`);
  `run_scale` aggregates node/edge P/R/F1 + merge counts, `--json` is deterministic
  (`AggregateFidelity.to_json`, sorted keys, `scale.py:266-268`).
- Fidelity JSON already exists machine-readably (`reconstruct fidelity --json`,
  `cli.py:994`); `bench score`/`bench report` are pure set math
  (`docs/BENCHMARK.md:62-72`).
- Pin/floor conventions to mirror: the golden run id pin
  (`tests/test_golden_run.py:189-194`, byte-for-byte reproduction `:171-185`) and the
  coverage `fail_under = 93` floor with its stated-reason comment
  (`pyproject.toml:100-106`).
- Roadmap-text corrections: (a) the epic names `enterprise-sim eval e2e`, but the CLI
  layout argues for `reconstruct e2e` (see decision 1); (b) "runs in CI on every PR"
  does not exist yet in any form — `docs/RECONSTRUCT.md:301`'s claim that CI exercises
  the keyless smoke is stale; (c) `reconstruct scale` already varies seeds implicitly —
  the matrix delta is an explicit seeds axis + committed baselines + CI wiring, not a
  new harness.

## What (scope)

- In scope:
  1. `enterprise-sim reconstruct e2e` — build→fidelity→reason→report into one output
     dir, plus a machine-readable `summary.json`; `--keyless-smoke` mode;
     `scripts/reconstruct_eval.sh` becomes a thin delegation shim.
  2. Committed score baselines (`evals/baselines/*.json`) + `enterprise-sim reconstruct
     baseline check|update` with per-cell tolerance semantics.
  3. An explicit seeds axis on `reconstruct scale` and a standing keyless matrix
     (specs × seeds) with a committed baseline and a runtime bound.
  4. A PR-CI `e2e-smoke` job (keyless) and a manual-dispatch keyed workflow that
     uploads the leaderboard/attribution artifact.
  5. Judge calibration as a **thin slice** riding the keyed workflow (see decision 6).
- Out of scope (explicitly):
  - Any `enterprise_sim/core/**` change (nothing here needs an engine primitive).
  - New office formats, new runners, new reasoning types (E4).
  - Cost/scale optimization, prompt-cache measurement (E5).
  - Coverage upload/badges; publishing eval results anywhere but GitHub artifacts.
  - A full judge-calibration harness (multi-artifact judging + correlation statistics)
    — deferred, see Open questions.
  - Recording new cassettes or extending cassette coverage (spec 0002 owns that).

## Design sketch

### 1. CLI home: `enterprise-sim reconstruct e2e` (not `eval e2e`)

The ROADMAP suggests `eval e2e`, but `eval` is a flat command whose positional argument
is a run dir (`cli.py:1848-1849`), and `enterprise-sim eval runs/golden/…` is a
documented CLAUDE.md dev-loop surface. Converting `eval` into a subparser group would
either break that call shape or require ambiguous positional-vs-subcommand dispatch.
`reconstruct` is already the group housing 4 of the 6 steps the chain runs
(build/fidelity/reason/report, `cli.py:825-830`), the shell script is literally named
`reconstruct_eval.sh`, and `docs/RECONSTRUCT.md` is where the workflow is documented.
**Decision: `reconstruct e2e`**, registered via `_add_reconstruct_e2e_parser` next to
its siblings. This revises the epic's suggested spelling, not any D-decision.

New module `enterprise_sim/reconstruct/e2e.py` (so the logic is testable without
argparse), driving the existing library functions in-process — no subprocess fan-out:

```
run_e2e(out_dir, *, run_dir=None, backend="anthropic_api", model=…, limit=None,
        keyless_smoke=False, use_bedrock=False, aws_region=None) -> E2EResult
```

- Step 0: fresh golden run from `examples/golden.toml` into `out/runs/` when `run_dir`
  is None (exactly what the script does, `reconstruct_eval.sh:60-64`) — **no new
  pinned config**; the golden pin is reused, not duplicated.
- Steps: `bench generate` → `reconstruct build` → `fidelity --json` → three reason
  slots → `report`, writing the same artifact set the script writes (`bench.jsonl`,
  `recon/`, `fidelity.json`, `pred.{oracle,reconstructed,rag}.jsonl`,
  `attribution.md`) plus `summary.json`: a sorted-keys JSON of {mode, backend, model,
  run_id, fidelity headline metrics, per-system answer F1, gaps} — the input
  `baseline check` consumes.
- `--keyless-smoke`: forces `backend="fake"` and substitutes the keyless RAG
  prediction for all three slots, mirroring `reconstruct_eval.sh:81-90`, with the same
  loud "wiring stand-ins, NOT an eval" note in `summary.json` (`"mode":
  "keyless-smoke"`) and stderr.
- Keyed path: parity with the script's flags (`--run --backend --model --limit`),
  plus E1's `--use-bedrock`/`--aws-region` passed through to the graph-agent slots
  (the script predates E1 and lacks them — this is the Bedrock parity gap `e2e`
  closes).
- `scripts/reconstruct_eval.sh` becomes a ~5-line shim: print a deprecation pointer,
  `exec uv run enterprise-sim reconstruct e2e "$@"` after flag translation
  (`--keyless-smoke`, `-o`, `--run`, `--backend`, `--model`, `--limit` map 1:1).
  Delete the shim in a later release (open question).

**Keyless smoke rides the `fake` backend, not cassettes.** Considered and rejected
using E2's cassettes for the smoke: (a) cassette keys are exact `request_key` hashes
of prompt+schema+model+temperature (`tests/llm_stubs.py:170-199`), and e2e prompts are
derived from the live golden corpus — any corpus or prompt-assembly change would
invalidate the whole recording set; spec 0002 deliberately confined cassettes to
*frozen literal fixtures* so the golden pin never invalidates them (ROADMAP:76-77).
(b) The oracle/reconstructed slots run the `claude-agent-sdk` graph agent, which does
not route through `LLMClient`'s cache, so cassettes cannot cover them at all. (c) The
extract/resolve/rag scenario cassettes are still unrecorded (spec 0002 status). The
fake backend is deterministic, dep-free, and — thanks to the structural extractor —
produces fidelity numbers that are real regression signal, which is what baselines
need. Cassettes stay scenario-level in `tests/test_llm_cassettes.py`.

### 2. Baselines: `evals/baselines/*.json` + `reconstruct baseline check|update`

A **baseline cell** = one (config/matrix, backend, seed) point with pinned metrics.
New top-level dir `evals/baselines/` (tracked eval state, not tests; referenced by
both PR CI and the keyed workflow; add a repo-map row to CLAUDE.md). One JSON file
per cell, schema (pydantic model in `enterprise_sim/reconstruct/baseline.py`):

```json
{
  "schema": 1,
  "cell": "golden-fake",
  "backend": "fake",
  "source": "enterprise-sim reconstruct baseline update --cell golden-fake",
  "config": "examples/golden.toml",
  "seed": 7,
  "mode": "exact",
  "tolerance": 0.0,
  "metrics": {
    "node_f1": 0.0, "node_precision": 0.0, "node_recall": 0.0,
    "edge_f1": 0.0, "edge_precision": 0.0, "edge_recall": 0.0,
    "provenance_f1": 0.0, "over_merges": 0, "under_merges": 0,
    "reconstructed_nodes": 0, "reconstructed_edges": 0
  },
  "reason": "initial baseline (spec 0003 slice 2)"
}
```

(Metric values above are placeholders; `update` fills real ones.) Committed cells:

| Cell file | Contents | Mode |
|---|---|---|
| `golden-fake.json` | golden-run fidelity, fake backend | `exact`, tolerance 0.0 |
| `matrix-fake.json` | per-cell + aggregate matrix fidelity (slice 3) | `exact`, tolerance 0.0 |
| `golden-keyed.json` | keyed answer-F1 per system + gaps + keyed fidelity | `warn`, tolerance 0.05 |

Tolerance semantics:

- **`exact` (fake backend)**: metrics are pure functions of a byte-reproducible run
  (`test_golden_run.py:171-185`) and a pure scorer, so the honest comparison is
  equality. Values are stored and compared rounded to 6 decimals (guards against
  last-ulp noise from e.g. summation reorder in a refactor without hiding any real
  metric movement); `tolerance: 0.0` means "differ at 6 dp ⇒ fail". `check` exits
  non-zero on any exceedance.
- **`warn` (keyed)**: answer-F1 from live models is nondeterministic; comparison is
  `abs(current − baseline) > tolerance ⇒ warn` (absolute F1 points, default 0.05).
  `check` prints the drift table and exits 0 unless `--strict` is passed. Keyed cells
  are never evaluated in PR CI (no keyed numbers exist there); they run in the keyed
  workflow, which never blocks PRs.

CLI: `enterprise-sim reconstruct baseline check [--cell NAME|all] [--against DIR]
[--strict]` and `… baseline update [--cell NAME|all] [--reason TEXT]`.

- `check` on a fake cell **regenerates** the cell in a temp dir (golden run + fake
  reconstruct + fidelity; matrix likewise) and compares — no stored intermediate
  state. `check --against out/` on a keyed cell reads an existing e2e output dir's
  `summary.json` instead (you cannot regenerate keyed numbers keylessly).
- `update` regenerates fake cells and rewrites the file (requiring `--reason`, which
  lands in the file); for the keyed cell it copies metrics from `--against DIR`.
- **Legitimate update convention** (mirrors the golden pin and `fail_under`,
  `pyproject.toml:102-105`): a change that deliberately moves a metric runs
  `baseline update --reason "…"` **in the same commit** as the code change, and the
  spec/docs note why. `check` failing on main means an unreviewed behavior change.
- Absent keyed baseline: until the first keyed workflow run supplies numbers,
  `golden-keyed.json` is not committed and `check` reports the cell as
  "unseeded — skipped" (not an error), so the harness lands keyless-first.

Gate coverage: keyless unit tests for compare semantics (exact pass/fail at 6 dp,
warn vs strict, unseeded skip) on in-memory fixtures, plus **one** gate test that
regenerates the `golden-fake` cell and asserts it matches the committed file (cost ≈
one golden run + fake reconstruct, the same work several existing tests already do —
gate stays within budget). The matrix cell is checked in the CI e2e-smoke job, not
the gate (see decision 4), with `make` parity so it's one local command.

### 3. Matrix: explicit seeds axis on `reconstruct scale`

Extend `scale` (CLI + `scale.py`) with `--seeds N,N,…` (default: current behavior,
i.e. the single `--seed` base): cells = `default_run_specs(count)` × seeds, each cell
a `RunSpec` with the catalog entry's label suffixed `-s<seed>`. `run_scale` is
unchanged in shape; `default_run_specs` gains a sibling
`matrix_run_specs(count, seeds) -> list[RunSpec]`.

Standing keyless matrix = **first 3 catalog specs × seeds {7, 107} = 6 cells**
(engineering-startup, retail-startup, engineering-small — two archetypes, two size
bands, two seeds; `scale.py:127-134`). Small on purpose: the matrix must complete
well inside the CI job bound (acceptance: the whole e2e-smoke job < 10 min on a
GitHub runner; the matrix command itself < 5 min). If a startup+small cell set proves
faster than expected, growing it is a baseline-update, not a design change.

Keyless scope: fake-backend fidelity over the matrix is fully keyless and is what
`matrix-fake.json` pins. Keyed scope: the same command with `--backend
anthropic_api|bedrock` is a keyed matrix; it is an *optional input* of the keyed
workflow (off by default — extraction over 6 corpora is the expensive part), not part
of the standing PR/dispatch defaults.

### 4. CI topology: gate untouched; a parallel PR job; a non-blocking keyed workflow

- **`scripts/gate.sh` is unchanged.** Adding an e2e run to the gate would multiply
  the ~20s local loop several-fold for signal the test suite mostly already carries.
  The only gate delta is the new keyless tests from slices 1–3 (the golden-cell
  baseline test ≈ 1–2s; unit tests negligible).
- **New `e2e-smoke` job in `.github/workflows/ci.yml`**, parallel to `quality` (so PR
  wall-clock is bounded by the slower job, not the sum): `uv sync --extra dev`, then
  `uv run enterprise-sim reconstruct e2e --keyless-smoke -o /tmp/e2e` and
  `uv run enterprise-sim reconstruct baseline check --cell all`. Runs on every PR.
  Rationale vs. joining gate.sh: preserves "gate output = quality job output" (the
  script stays the single source of truth for *its* checks, as today, where
  `import_smoke` is likewise a separate step, `ci.yml:35-38`); rationale vs. a
  schedule: the whole point is catching fidelity regressions *on the PR that causes
  them*, and the job is cheap and keyless. Local parity: `make e2e-smoke` runs the
  same two commands, and the golden-cell gate test means most baseline drift is
  caught by the local gate anyway.
- **New `.github/workflows/eval-keyed.yml`**: `on: workflow_dispatch` (inputs:
  `backend` choice `anthropic_api` (default) | `bedrock`; `model`; `limit` default
  16; `matrix` boolean default false), cron commented out until cost per run is
  observed. Steps: checkout, `uv sync --extra dev --extra bench`, `reconstruct e2e
  --backend $backend --model $model --limit $limit -o eval-out/` (plus
  `--use-bedrock`-routed reason slots when `backend == bedrock`), `reconstruct
  baseline check --cell golden-keyed --against eval-out/` (warn mode), `eval --judge`
  step (slice 6), then `actions/upload-artifact` of `eval-out/` (attribution.md,
  summary.json, fidelity.json, predictions, judge verdict). Secrets:
  `ANTHROPIC_API_KEY`; `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION` used
  only on the bedrock path. **Never blocks PRs**: no `pull_request`/`push` trigger,
  and it is not a required check. **Default credential path: 1P `anthropic_api`** —
  E1's Bedrock wiring is complete but its *live* validation is still pending AWS
  creds (ROADMAP:18-21), and every recorded number in `docs/RECONSTRUCT.md` came from
  1P runs, so 1P keeps the first keyed baseline comparable; Bedrock is one dispatch
  input away once creds exist.

### 5. Docs: regression tracking replaces hand-recorded snapshots

`docs/RECONSTRUCT.md`'s Results sections stay as the historical narrative, but gain a
preamble: current numbers live in `evals/baselines/` + the keyed workflow's artifacts;
the "paste numbers into `_TBD_` cells" instruction (`:372-373`) and the stale "This is
what CI exercises" line (`:301`) are replaced with the `reconstruct e2e` / `baseline`
workflow. `docs/GOLDEN_RUN.md` gains one line: a golden-pin regen requires
`reconstruct baseline update` in the same commit (the baselines are functions of the
pinned run). CLAUDE.md: dev-loop line for `reconstruct e2e --keyless-smoke`, repo-map
row for `evals/`.

### 6. Judge calibration: thin slice, honestly scoped

`eval --judge` samples **one** artifact per run (`judge_sample`, `cli.py:294`) and the
fake-backend verdict is canned — a keyless correlation is meaningless, and a real one
needs a keyed multi-artifact judging harness that doesn't exist. **Thin slice**: the
keyed workflow additionally runs `enterprise-sim eval <run> --judge --backend
$backend` on the e2e run dir and includes the verdict in the uploaded artifact next
to the structural metrics, and `docs/RECONSTRUCT.md` gains a short "reading the judge
next to structural metrics" note. The full fixed-artifact-set calibration harness is
**deferred** (see Open questions) — it is keyed, manual, and worth its own small spec
once a few keyed-workflow artifacts exist to correlate.

## Invariant impact

- **Keyless gate**: no new dependencies in either extra; `reconstruct e2e
  --keyless-smoke`, `baseline check|update` (fake cells), and the matrix import only
  dev-extra modules (all heavy deps stay lazily imported exactly as the wrapped
  commands do today). No network, no key, no wall-clock in any new default path. Gate
  runtime grows only by the slice-1/2/3 keyless tests (~2–3s bound; acceptance below).
- **Determinism**: fake-cell baselines are exactly reproducible — they are pure
  functions of the byte-reproducible golden/matrix runs (D10/D31,
  `test_golden_run.py:171-185`) and the pure fidelity scorer; `exact` cells enforce
  equality at 6 dp. `summary.json` is sorted-keys JSON with no timestamps in
  keyless-smoke mode (keyed mode may record model/backend metadata; still no
  wall-clock in the fake path).
- **Core purity**: zero `enterprise_sim/core/**` changes. New code:
  `enterprise_sim/reconstruct/{e2e.py,baseline.py}`, `scale.py` seeds axis, `cli.py`
  parsers, `evals/baselines/`, workflows, Makefile, docs.
- **Golden-run pin**: the e2e smoke **reuses `examples/golden.toml`** via a fresh run
  (as `reconstruct_eval.sh:60-64` does) — no second pinned config to drift. The
  coupling is explicit: baselines depend on the pin, so a deliberate golden regen
  updates `tests/test_golden_run.py`, `docs/GOLDEN_RUN.md`, **and** `reconstruct
  baseline update` in the same commit (documented in both docs).
- **Keyed-code gating**: keyed e2e paths reuse the already-gated runners (missing key
  ⇒ clean exit 2, `cli.py:1398-1400,468-470`); no new lazy dep, so
  `scripts/import_smoke.py` needs no change. The keyed workflow uses repo secrets
  only; `--limit` bounds cost (D13); no run-cost is incurred by PR CI.

## Slices (each independently gate-green)

1. **`feat(reconstruct): one-command e2e eval (reconstruct e2e)`** —
   `enterprise_sim/reconstruct/e2e.py` + CLI wiring + `summary.json`;
   `--keyless-smoke`; `scripts/reconstruct_eval.sh` becomes a delegation shim.
   Tests: keyless test runs `run_e2e(keyless_smoke=True)` into tmp, asserts the
   artifact set exists, `summary.json` parses with `mode == "keyless-smoke"`, and two
   invocations produce identical `summary.json`; CLI arg-surface test. Docs:
   RECONSTRUCT.md "Reproducing it" section.
2. **`feat(reconstruct): score baselines + baseline check/update`** —
   `baseline.py` (schema, compare, regenerate golden cell), CLI, committed
   `evals/baselines/golden-fake.json`. Tests: compare-semantics units (exact/warn/
   strict/unseeded), golden-cell regeneration matches the committed file. Docs:
   update convention (same-commit + `--reason`), CLAUDE.md repo-map row.
3. **`feat(reconstruct): seeds axis for scale + standing matrix baseline`** —
   `--seeds`, `matrix_run_specs`, committed `evals/baselines/matrix-fake.json`,
   `baseline check --cell matrix-fake` regenerates and compares, `make e2e-smoke`
   target. Tests: seeds-axis unit (cell labels/seeds deterministic), matrix cell
   check keyless (marked or bounded so the gate budget holds — if regeneration of 6
   cells exceeds ~5s, the gate test covers a 2-cell sub-matrix and the full check
   lives in CI only; decide by measurement).
4. **`ci: keyless e2e-smoke job on every PR`** — second job in `ci.yml` running
   `reconstruct e2e --keyless-smoke` + `baseline check --cell all`; gate.sh
   untouched. Acceptance is the job itself going green.
5. **`ci: manual-dispatch keyed eval workflow`** — `.github/workflows/eval-keyed.yml`
   (dispatch inputs, 1P default, Bedrock path, warn-mode baseline compare, artifact
   upload). Keyless-verifiable acceptance: workflow YAML lints (`actionlint` or
   equivalent review), no PR trigger present; live acceptance is one dispatched run
   by the key owner (pending, like 0001/0002's keyed items).
6. **`docs(reconstruct): baseline-driven results + judge thin slice`** — RECONSTRUCT.md
   rewrite of the Results preamble + stale-CI-claim fix, GOLDEN_RUN.md coupling note,
   `eval --judge` step added to the keyed workflow with the verdict in the artifact,
   judge-interpretation note. (Pure docs + workflow-step; conventional type `docs`,
   or `feat(ci)` if the workflow step lands separately.)

Review routing note: slices 2–4 touch fidelity-scoring adjacency, the golden-pin
coupling, and CI topology — route their diffs to `adversary` per CLAUDE.md triggers.

## Test & validation plan

- Keyless (gate): slice tests above; determinism of `summary.json`; baseline compare
  semantics; golden-cell equality; CLI surface tests mirroring existing
  `test_backend_enum_matches_backend_factory`-style pins where flags are shared.
- Keyless (CI job): the e2e smoke + full baseline check on every PR.
- Keyed/live: the dispatch workflow (owner-run); reuses existing `requires_llm_runner`
  gating in tests — no new keyed tests needed. No new lazy deps ⇒ `import_smoke.py`
  unchanged.
- Golden pin: unchanged by this spec (no generation-path edits). If a *later* change
  regens the pin, baselines regen in the same commit (documented).

## Acceptance criteria

- [x] `uv run enterprise-sim reconstruct e2e --keyless-smoke -o /tmp/e2e` exits 0 and
      writes `attribution.md`, `fidelity.json`, `summary.json`, `bench.jsonl`,
      `recon/`, `pred.{oracle,reconstructed,rag}.jsonl` under `/tmp/e2e`; running it
      twice yields byte-identical `summary.json` (`diff` clean). *(Verified slice 1;
      re-verified at closeout.)*
- [x] `bash scripts/reconstruct_eval.sh --keyless-smoke -o /tmp/e2e2` still works
      (delegates to the CLI) and prints the deprecation pointer.
- [x] `uv run enterprise-sim reconstruct baseline check --cell all` exits 0 on a clean
      checkout; after `sed`-perturbing a metric in
      `evals/baselines/golden-fake.json`, it exits non-zero naming the metric.
- [x] `uv run enterprise-sim reconstruct baseline update --cell golden-fake --reason x`
      rewrites the file to a state where `check` passes and `git diff` shows only the
      `reason` change on a clean checkout.
- [x] `uv run enterprise-sim reconstruct scale --runs 3 --seeds 7,107 --json` emits 6
      per-run rows, deterministically (two invocations `diff` clean).
- [x] `make e2e-smoke` runs the smoke + full baseline check locally; the CI `e2e-smoke`
      job runs the same commands and the `quality` job's runtime is unchanged (gate.sh
      untouched). *(Local `make e2e-smoke` green; CI job green-on-PR is demonstrable
      only once a PR runs it — the job is committed in `ci.yml`.)*
- [x] `git grep -n "pull_request" .github/workflows/eval-keyed.yml` returns nothing
      *but the header comment that documents the absence of a PR trigger*; the workflow
      has `workflow_dispatch` with a `backend` input defaulting to `anthropic_api` and
      uploads `eval-out/` via `actions/upload-artifact`. *(There is no `pull_request:`
      trigger; the sole grep hit is the explanatory comment.)*
- [~] Keyed (owner, pending creds): one dispatched `eval-keyed` run succeeds, its
      artifact contains `attribution.md` + `summary.json` + a judge verdict, and
      `baseline update --cell golden-keyed --against …` seeds
      `evals/baselines/golden-keyed.json`. *(Pending — the workflow + judge step +
      seed procedure are committed and docs describe them; the live dispatch awaits
      1P/AWS creds, like 0001/0002's keyed items.)*
- [x] Docs updated: `docs/RECONSTRUCT.md` (no stale CI claim; baseline workflow +
      judge note), `docs/DEVELOPMENT.md` (e2e + baselines + workflows, `reconstruct_eval.sh`
      demoted to a shim), `docs/GOLDEN_RUN.md` (pin↔baseline coupling), `CLAUDE.md`
      (dev loop + repo map), `specs/ROADMAP.md` E3 references this spec.
- [x] `./scripts/gate.sh` green on every commit; full-suite runtime stays within ~25s
      on the reference machine (the +2–3s budget for new keyless tests).

## Review findings & resolutions

An adversarial review of the baseline/e2e slices (2–6) raised six findings (F1–F6),
all resolved in one fix round (`fix(reconstruct): self-enforcing baselines,
registry-driven check, keyed answer-F1 pinning`). The committed fake cells are
**byte-stable** — the fix adds check-time enforcement and a keyed metric-key concept,
but no new fields to the fake-cell file format, so `golden-fake.json` /
`matrix-fake.json` are unchanged on disk (verified: `git status` clean after the fix).

- **F2 (worst) — baselines weren't self-enforcing.** A committed cell's
  `mode`/`tolerance`/`backend` and metric-key set were declarative text a hand-edit
  could launder (bump the tolerance, flip `exact`→`warn`, delete a metric) with
  `check` still green. Fix: `baseline.identity_mismatches(cell, spec, current_keys)`
  compares the file against the code-defined `CellSpec` registry (and, for fake cells,
  the full key set the live regeneration produces) and `check` FAILs naming the
  divergent field, with the message "identity fields are declarative documentation, the
  registry is authoritative". A metric present in the regeneration but missing from the
  file fails as "no silent shrinkage". Probes A/B/C (tolerance bump, mode flip, metric
  deletion) now all exit 1.
- **F3 — `--cell all` globbed the directory.** It iterated `evals/baselines/*.json`, so
  a registered cell whose file vanished was silently absent and a stray unregistered
  `.json` crashed with a raw `KeyError` from `regenerate_fake_metrics(CELL_SPECS[name])`.
  Fix: `check --cell all` iterates the `CELL_SPECS` registry — a missing registered fake
  cell FAILs ("registered baseline cell missing: <path>"), an unseeded keyed cell keeps
  the exit-0 skip notice, and a stray file FAILs with a clear "unregistered baseline
  file …" message (probe D, previously a traceback). A single unknown `--cell NAME` is
  likewise a clear error, not a `KeyError`.
- **F1 — keyed cells under-pinned.** The keyed `golden-keyed` cell pinned only the
  fidelity block, discarding the answer-F1/gap headline the keyed eval exists to
  measure. Fix: `CellSpec` gains a `metrics_shape` (`"fidelity"` / `"fidelity+answers"`
  / `"matrix"`); `expected_metric_keys` and `metrics_from_summary(spec, summary)` extract
  accordingly, so a keyed cell pins fidelity + `answer_f1.{oracle,reconstructed,rag}` +
  `gaps.{understanding,reasoning,total}` flattened from `summary.json`. Fake cells are
  unchanged (fidelity only). `docs/DEVELOPMENT.md`'s keyed-cell paragraph updated.
- **F4 — no provenance guard on the keyed `--against` path.** `update`/`check --against`
  would seed/compare a keyed cell from any `summary.json`, including a keyless-smoke
  stand-in or a different backend's run. Fix: `keyed_summary_problem(spec, summary)`
  refuses (clear error, nonzero exit) a summary whose `mode == "keyless-smoke"` or whose
  `backend != spec.backend`. Probe F confirms both refusals on `update`; a seeded cell's
  `check --against` refuses likewise.
- **F5 — smoke marker only on the machine-readable side.** `summary.json` carried the
  loud stand-ins note but the human-readable `attribution.md` did not, so a report file
  that outlived its dir read as a real eval. Fix: `e2e._attribution_markdown` prepends a
  one-line `> **KEYLESS SMOKE** — …` banner (matching the summary note) at the write
  site in smoke mode; `render_markdown` stays a pure, unbannered projection reused by the
  keyed path and `reconstruct report`.
- **F6 — workflow script-injection hygiene.** `eval-keyed.yml` interpolated
  `${{ inputs.* }}` directly inside `run:` shells. Fix: every dispatch input is routed
  through an `env:` block and referenced as `"$BACKEND"`/`"$MODEL"`/`"$LIMIT"` in the
  scripts (no `${{ }}` inside any `run:`); `if:`/`env:`/artifact-`name:` contexts, which
  are not shell-injection surfaces, are unchanged. YAML re-validated structurally.

## Open questions (resolved)

- **Delete `reconstruct_eval.sh` outright vs. shim?** **Resolved: shim.** Slice 1
  made it a thin delegation shim that prints the deprecation pointer and `exec`s
  `reconstruct e2e`. `docs/RECONSTRUCT.md` and `docs/DEVELOPMENT.md` now point at the
  CLI and describe the script only as a deprecated shim; the actual file deletion is
  left as a trivial future cleanup once no one's muscle memory reaches for it (no new
  spec needed — just remove the shim + its one doc mention).
- **Matrix size/cells in the gate vs. CI-only.** **Resolved: gate checks the
  `golden-fake` cell only; the full 6-cell `matrix-fake` check lives in the CI
  `e2e-smoke` job** (`baseline check --cell all`) and `make e2e-smoke`, keeping the
  ~20s gate budget. The matrix (`baseline check --cell matrix-fake`) regenerates 6
  fake cells and is verified in slice 3 to run inside the CI job bound, not the gate.
- **Cron cadence for `eval-keyed`.** **Resolved: dispatch-only for now.** The cron is
  intentionally omitted (commented rationale in `eval-keyed.yml`) until two manual
  runs establish per-run cost; add a weekly cron with `--limit 16` and the observed
  cost then. Tracked as a follow-up on the first keyed dispatch.
- **Keyed tolerance value (0.05 F1).** **Resolved: keep 0.05, warn-only.** The
  `golden-keyed` cell is `warn` mode, tolerance 0.05; revisit (tighten) once ≥3 keyed
  artifacts establish the real run-to-run spread. Encoded in `baseline.py`'s cell spec.
- **Judge calibration harness.** **Resolved: deferred to its own spec.** This spec
  ships only the thin slice — the keyed workflow runs `eval --judge` on the e2e run
  and files the single-artifact verdict (`judge.txt`) in the artifact next to the
  structural metrics, and both `docs/RECONSTRUCT.md` and the workflow explain it is a
  qualitative reading, not a calibrated score. The full fixed-artifact-set,
  multi-artifact correlation harness needs several keyed artifacts to correlate
  against and is worth its own small spec once ~3 keyed runs exist; the thin slice
  collects exactly that data.
- **Should `reconstruct e2e` default `--backend` to `fake` instead of
  `anthropic_api`?** **Resolved: default `anthropic_api`** (matches the script's
  keyed semantics — e2e *is* the keyed crew command; `--keyless-smoke` is the explicit
  keyless flag). The missing-key path exits cleanly via the existing gated runners.
