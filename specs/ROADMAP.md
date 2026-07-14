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

**Status: done (live validation pending AWS creds)** — Bedrock is wired end-to-end across
every LLM-touching surface (backend deps, `run --backend bedrock`, model-id/pricing
mapping, region/profile config, keyless request-path tests, `--use-bedrock` runners, docs);
`scripts/bedrock_smoke.py` is the cred-gated live validation command. See the spec's
acceptance criteria for the live-run items still pending an AWS account.

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

## E2 — Local testing hardening: backend contract + record/replay (P0) — `specs/0002-local-testing-hardening.md`

**Status: done (cassette recordings pending a keyed run)** — all seven slices landed: a
parametrized backend-contract suite over all four backends with a completeness pin
(`tests/test_backend_contract.py`), cassette record/replay built on `ResponseCache`
(`tests/test_llm_cassettes.py` + `tests/llm_stubs.py`, replay keyless / recording keyed via
`make record-cassettes`), and coverage in the gate with a `fail_under` floor. The
extract/resolve/rag cassettes are unrecorded — a keyed step for the owner; until then the
three scenario tests skip keyless and the round-trip self-test covers the replay/drift
machinery. See the spec's acceptance criteria for the pending-recording item. Note the
overlap with spec 0001, which already delivered part of this epic: the shared
`_AnthropicSDKBackend` request path has keyless stubbed-client coverage
(`tests/test_llm_sdk_path.py`; the original "entire request path is `pragma: no cover`"
claim here is stale), `scripts/import_smoke.py` pins the SDK's `Messages.create` signature,
and config/CLI/`build_backend` lockstep is enforced
(`test_backend_enum_matches_backend_factory`, plus the `ModelConfig.backend = "fake"`
default fix).

Make the real-LLM paths testable without a key, so regressions in prompt assembly,
tool-forced structured output, error normalization, and answer parsing/resolution are
caught by the keyless gate instead of a live run. Remaining work, designed in the spec:

- **Backend contract suite**: one parametrized suite asserting `Backend`-protocol
  semantics across *all four* backends (`fake` real; SDK pair stubbed; `claude_cli` via a
  seam stub), with a completeness pin so a future backend must join it — the honest delta
  over `test_llm_sdk_path.py`, which stays as the SDK-specific request-shape pin.
- **Record/replay fixtures ("cassettes")**: capture real responses once (keyed, manual),
  replay them keyless in CI, so `reconstruct extract/resolve` and the RAG answer path get
  true regression tests rather than skips. Built on the existing `ResponseCache` (D31) —
  no new recording dependency; replay is offline and byte-stable, and cassettes use frozen
  literal fixtures so the golden-run pin never invalidates them.
- **Coverage visibility**: coverage reporting in `scripts/gate.sh` via pytest-cov
  (one-line report first, then a floor) so untested-but-live code is visible; the gate's
  ~20s budget holds.

## E3 — End-to-end eval hardening (P0) — `specs/0003-e2e-eval-hardening.md`

**Status: spec approved** — design + slices in the spec. Notable deltas from the text
below (audit 2026-07-14): the CLI home is `enterprise-sim reconstruct e2e` (the flat
`eval <run>` command can't grow subcommands without breaking its documented surface);
nothing in CI runs the keyless smoke today (docs/RECONSTRUCT.md's claim is stale); the
keyless smoke rides the `fake` backend, not E2's cassettes (cassette keys would be
invalidated by any corpus/prompt change, and the agent-SDK reason slots bypass the
cache); judge calibration is a thin slice riding the keyed workflow, with the full
harness deferred.

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
