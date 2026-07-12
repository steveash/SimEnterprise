# ROADMAP — epic-level feature requests

Ordered by priority. Each epic is a feature request with enough context to spawn a
`specs/NNNN-*.md` spec (see `README.md` in this folder for the workflow). Status here is
epic-level; per-feature status lives in the individual specs.

Context: v1 (markdown-only, M1–M7 of PLAN.md) is done — world → events → corpus → gold KG,
with the golden run as the pinned acceptance artifact, plus the `bench` and `reconstruct`
eval harnesses. The near-term theme is **hardening the foundation**: run everything
without the 1P Anthropic API (Bedrock), and make local testing and end-to-end evaluation
strong enough that new features (office formats, new modalities, scale) land safely on top.

---

## E1 — First-class Amazon Bedrock support (P0) — `specs/0001-bedrock-first-class.md`

**Status: in-progress (spec written)**

Run every LLM-touching path — corpus generation, `eval --judge`, `bench run`,
`reconstruct build/reason`, and the graph-agent runner — against Amazon Bedrock with only
AWS credentials, no `ANTHROPIC_API_KEY`. A `BedrockBackend` already exists (the official
SDK's `AnthropicBedrock` client, same request path as the API backend) but it is not
usable end-to-end today. Known gaps, from a code audit (2026-07-12):

1. **Missing runtime dependency**: `AnthropicBedrock` requires the `anthropic[bedrock]`
   extra (boto3/botocore); nothing declares it, so the backend crashes on first real call.
   `scripts/import_smoke.py` only constructs the `anthropic_api` backend, so CI can't see it.
2. **`enterprise-sim run` cannot use any real backend**: `assembly/runner.py` always
   builds a `fake` client; the config's `[model].backend` field (which already offers
   `bedrock`) is silently ignored. Real-backend corpus generation needs explicit plumbing
   (a `run --backend` flag; keyless-by-default preserved).
3. **Model-ID and pricing mapping**: `core/llm/pricing.py` is keyed on 1P ids
   (`claude-sonnet-4-6`); Bedrock wants inference-profile ids
   (`us.anthropic.claude-sonnet-4-6…-v1:0`). Cost accounting/ceiling (D13) must work for both.
4. **Agent-SDK paths**: the graph-agent runner (`claude-agent-sdk`) and the RAG runner
   need Bedrock wiring (`CLAUDE_CODE_USE_BEDROCK=1` + region config) and cred-gated skips.
5. **No Bedrock test or smoke anywhere**; region/profile configuration is undocumented.

Acceptance shape: a documented `AWS_*`-only quickstart, keyless unit coverage of the
Bedrock request path (mock transport), import smoke covering `AnthropicBedrock`, and a
cred-gated live smoke.

## E2 — Local testing hardening: backend contract + record/replay (P0)

Make the real-LLM paths testable without a key, so regressions in prompt assembly,
tool-forced structured output, error normalization, and retry/cost handling are caught by
the keyless gate instead of a live run.

- **Backend contract suite**: one parametrized test suite asserting the `Backend` protocol
  semantics (structured output conforms to schema, envelope parsing, transient-vs-terminal
  error mapping, usage accounting) that runs against `fake` keylessly and against
  `anthropic_api`/`bedrock` via a mocked SDK transport — today the entire
  `_AnthropicSDKBackend` request path is `pragma: no cover`.
- **Record/replay fixtures ("cassettes")**: capture real SDK responses once (keyed), replay
  them keyless in CI, so `reconstruct extract/resolve` and the RAG runner get true
  regression tests rather than skips. Determinism invariant (D31) preserved: replay is
  offline and byte-stable.
- **Coverage visibility**: add coverage reporting to the gate (report-only first, then a
  floor) so untested-but-live code like the SDK path is visible instead of invisible.
- **Config/CLI consistency**: `ModelConfig.backend` now accepts `fake` (fixed alongside
  this roadmap); keep config, CLI choices, and `build_backend` in lockstep with a test.

## E3 — End-to-end eval hardening (P0)

The eval loop (golden run → bench → reconstruct → attribution report) is the product's
proof of value; make it a first-class, regression-tracked harness instead of a script.

- **One-command e2e eval**: promote `scripts/reconstruct_eval.sh` into
  `enterprise-sim eval e2e` with a `--keyless-smoke` mode that runs in CI on every PR.
- **Score regression tracking**: persist benchmark/fidelity baselines (per seed + config)
  in-repo; fail or warn when a change moves answer-F1/fidelity beyond a tolerance —
  today docs/RECONSTRUCT.md numbers are hand-recorded snapshots.
- **Multi-seed / multi-scale eval corpora**: extend `reconstruct scale` into a small
  standing matrix (seeds × sizes) so metrics aren't overfit to the single golden slice.
- **Scheduled keyed CI**: a manual-dispatch (later cron) workflow that runs the keyed eval
  with repository secrets, publishing the leaderboard as a build artifact.
- **Judge calibration**: correlate LLM-as-judge scores with structural metrics on a fixed
  artifact set so `eval --judge` output is interpretable.

## E4 — Office formats & new modalities to done (M8–M10 completion) (P1)

Producer modules for `word_docx` (+ OOXML comment spike), `pptx`, `jira`, `outlook`,
`email_eml`, and `calendar_ics` exist; none is wired into an acceptance-grade, pinned run
the way markdown is. Per modality: config binding (`deliverable.kind → producer`), a
golden-style pinned run + docs, corpus/KG provenance parity (mentions must ground into the
native format), and reconstruct-ability (the extractor can read it back). Native `.docx`
threaded comments remain the top technical risk (PLAN.md §5) — keep the spike-first rule.

## E5 — Scale & cost efficiency (P1)

Enterprise-scale runs (many departments, months of sim time) within a budget: cache
hit-rate and cost reporting per run (`manifest.json` already carries estimates), prompt
cache effectiveness measurement, concurrency tuning beyond the current bounded thread
pool, resumable/incremental runs, and a documented cost model per corpus size. Builds on
E1 (cheap bulk inference often means Bedrock batch/provisioned throughput).

## E6 — Graph-explorer productization (P2)

The Electron explorer works but sits outside the quality system: no CI (typecheck/vitest
job), stale metadata (fixed: repo homepage), no packaged releases, and its agent chat is
1P-key-only (should ride E1's Bedrock work). Lower priority than the Python core; keep it
green, not gold-plated.

---

### Adding to this file

New epics get a short section like the above: why it matters, what "done" looks like,
known constraints/pointers. Keep it epic-level — implementation detail belongs in the
feature spec.
