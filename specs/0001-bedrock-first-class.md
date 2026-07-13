# 0001 — First-class Amazon Bedrock support

Status: done (live validation pending AWS creds)
Epic: ROADMAP E1
Owner: unclaimed

## Why

The owner wants to run Enterprise Sim's real-LLM paths without a 1P Anthropic API key,
using AWS credentials via Amazon Bedrock. The architecture anticipated this — decision D9
("pluggable LLM: API/Bedrock/CLI, config-selected") and ARCHITECTURE.md §7 ("key vs
Bedrock is one dependency, two constructors") — and `BedrockBackend` exists in
`enterprise_sim/core/llm/backends.py` as a 4-line subclass of the shared
`_AnthropicSDKBackend`. But an audit (2026-07-12) shows it cannot actually be used
end-to-end. This spec closes the gap for every LLM-touching surface.

## Current gaps (audit findings)

1. **Undeclared runtime dep.** `anthropic.AnthropicBedrock()` requires the
   `anthropic[bedrock]` extra (boto3/botocore). Neither `pyproject.toml` extra declares
   it and `uv.lock` has no boto3, so the first live Bedrock call raises. This is exactly
   the class of bug `scripts/import_smoke.py` exists to catch (esim-sr3), but the smoke
   only constructs `LLMConfig(backend="anthropic_api")`.
2. **`enterprise-sim run` ignores the config's backend.** `assembly/runner.py::_client_for`
   always builds a `fake` client; `ModelConfig.backend` (which already includes `BEDROCK`)
   is decorative. There is no `--backend` flag on `run` (unlike `eval`, `bench run`,
   `reconstruct build`). So real-backend **corpus generation** — the core product — has no
   entry point from the CLI for *any* provider; Bedrock inherits that hole.
3. **Model ids & pricing.** `core/llm/pricing.py` keys costs on 1P ids
   (`claude-sonnet-4-6`, `DEFAULT_MODEL`). Bedrock expects inference-profile ids
   (e.g. `us.anthropic.claude-sonnet-4-6-…-v1:0`). Passing a 1P id fails on Bedrock;
   passing a Bedrock id breaks cost lookup, so the D13 cost ceiling and dry-run estimates
   silently degrade.
4. **No region/profile configuration surface.** `AnthropicBedrock()` is constructed with
   no arguments; region comes only from ambient AWS env. There's no way to set
   `aws_region` (or profile) in `[model]` config or `LLMConfig`.
5. **Agent-SDK and RAG runners are 1P-only.** The graph-agent runner
   (`benchmark/runners/graph_agent.py`, `claude-agent-sdk`) and RAG runner construct
   1P-keyed clients; keyed test gates check `ANTHROPIC_API_KEY` specifically, so
   AWS-cred-only environments skip everything with no Bedrock alternative.
6. **Zero Bedrock tests/smoke/docs.** The entire `_AnthropicSDKBackend` request path is
   `pragma: no cover`; no doc says how to run on Bedrock.

## What (scope)

- In scope: dependency fix; `run --backend` plumbing; model-id/pricing mapping;
  region/profile config; import-smoke + keyless unit coverage of the Bedrock request
  path; cred-gated live smoke; docs (README quickstart, docs/DEVELOPMENT.md section);
  graph-agent/RAG runner Bedrock mode.
- Out of scope: Bedrock batch inference / provisioned throughput (ROADMAP E5); the
  graph-explorer app's agent chat (ROADMAP E6); non-Anthropic Bedrock models.

## Design sketch

Slices, each independently gate-green:

1. **Deps + smoke** (`fix`): add `anthropic[bedrock]>=0.40` to the `bench` extra (keep the
   plain `anthropic` line for the API backend or fold into one extra spec). Extend
   `scripts/import_smoke.py` to `build_client(LLMConfig(backend="bedrock"))` and import
   the `AnthropicBedrock` symbol. CI already runs the smoke.
2. **Backend config surface** (`feat(llm)`): `BedrockBackend.__init__(*, aws_region: str | None = None,
   aws_profile: str | None = None, max_tokens=...)` passed through to `AnthropicBedrock(...)`;
   thread backend kwargs from `LLMConfig` (new optional fields) and `[model]` config.
   Defaults preserve today's ambient-env behavior.
3. **Model-id mapping + pricing** (`feat(llm)`): a small pure function
   `normalize_model_id(model: str) -> str` that maps Bedrock inference-profile ids to
   their 1P pricing key (regex on `…anthropic\.(claude-[a-z0-9-]+)-\d{8}-v\d+:\d+`), used
   by `pricing.py` lookups. Rather than default `BedrockBackend` to a hard-coded
   Bedrock-form model constant (a dated inference-profile id can't be verified offline,
   so a stale/wrong constant is worse than none), the Bedrock path **fails fast** when
   handed a non-Bedrock model id — see finding F2. Fully keyless-testable.
4. **`run --backend`** (`feat(cli)`): add `--backend {fake,anthropic_api,bedrock,claude_cli}`
   to `enterprise-sim run`, default **`fake`** (determinism invariant unchanged — a real
   backend is always an explicit opt-in, matching `_DEFAULT_BACKEND`'s comment). When a
   real backend is chosen, `llm_config_for(config, backend=…)` already carries model,
   concurrency, cost ceiling, and cache settings. Warn (don't fail) when the flag
   contradicts `[model].backend` so configs stay meaningful.
5. **Keyless request-path tests** (`test`): unit tests for `_AnthropicSDKBackend._call_tool`
   with a stubbed client object (no SDK import needed — it's duck-typed `Any`): system
   `cache_control` layering, forced tool-use extraction, `_normalize_sdk_error`
   429/5xx/Retry-After mapping, `_usage_from_sdk`. This removes most `pragma: no cover`
   from the shared path both backends use.
6. **Runners** (`feat(bench)`): graph-agent runner gains a Bedrock mode (set
   `CLAUDE_CODE_USE_BEDROCK=1` + region for the agent SDK subprocess/env); RAG runner just
   inherits the backend flag it already has. Broaden keyed-skip helpers: skip unless
   `ANTHROPIC_API_KEY` **or** (`--backend bedrock` and AWS creds present).
7. **Live smoke + docs** (`feat(scripts)`, `docs`): `scripts/bedrock_smoke.py` — one
   structured + one content call, cred-gated, never in CI by default; README/DEVELOPMENT
   quickstart: required AWS env, region, model-id examples, cost note.

Invariants check: keyless gate untouched (all live code stays lazily imported and gated);
determinism preserved (`run` default remains `fake`); no `core/` domain edits (this is the
LLM layer, which is core infrastructure — allowed); golden-run pin unaffected.

## Test & validation plan

- Keyless: model-id normalization, pricing lookup for Bedrock ids, backend kwargs
  plumbing, `run --backend fake` equivalence, stubbed-client request-path tests.
- Import smoke: `bedrock` client construction under `--extra bench` (CI, no creds).
- Live (cred-gated, manual): `scripts/bedrock_smoke.py`, then
  `enterprise-sim run examples/demo.toml --backend bedrock` and one
  `reconstruct build --backend bedrock` on a small corpus.

## Acceptance criteria

- [x] `uv sync --extra bench && uv run python scripts/import_smoke.py` constructs the
      Bedrock backend (fails today). *(slice 1: smoke now `build_client`s the `bedrock`
      backend and imports `AnthropicBedrock` + the boto3 signing stack.)*
- [~] With only AWS creds (no `ANTHROPIC_API_KEY`) and a Bedrock-form `[model].name`
      (`us.anthropic.claude-<family>-<YYYYMMDD>-v1:0`): `enterprise-sim run … --backend
      bedrock` produces a corpus; cost accounting shows non-zero, correctly priced usage
      for the Bedrock model id. A 1P model id under `--backend bedrock` instead **fails
      fast** at client build (dry-run included) with the inference-profile shape to set
      (finding F2) — it does not reach a live call.
      *Implemented (slices 3–4); the fail-fast half is keyless-tested, the live-corpus half
      is pending-live-validation — `scripts/bedrock_smoke.py` is the validation command.*
- [~] `bench run --runner rag --backend bedrock` and `reconstruct build --backend bedrock`
      work; graph-agent runner documented Bedrock mode works or has a spec'd follow-up.
      *Wired (slice 6: RAG/`reconstruct` via `--backend`, graph-agent via `--use-bedrock`);
      live behavior pending-live-validation — validate with `scripts/bedrock_smoke.py`.*
- [x] Keyless gate covers the shared SDK request path (no blanket `pragma: no cover` on
      `_call_tool` / error normalization / usage mapping). *(slice 5: stubbed-client tests.)*
- [x] README + docs/DEVELOPMENT.md contain a copy-pasteable Bedrock quickstart.
      *(slice 7: README Quickstart sub-block + docs/DEVELOPMENT.md Bedrock section.)*
- [x] `./scripts/gate.sh` green on every commit; no new network in default paths.

## Review findings & resolutions

An adversarial review of slices 1–5 raised seven findings (F1–F7). Fix round A
(`fix(run): honest backend defaults, self-describing run dirs, symmetric backend
warnings`) resolved F1/F3/F4; fix round B (`fix(llm): fail fast on non-Bedrock model
ids, warn on pricing fallback, pin SDK call signature`) resolves F2/F5/F6/F7.

Resolved in fix round A:

- **F1 — run dir not self-describing / backend collision.** The run id is a pure
  function of `(config, seed)` and does not fold in the render backend, so the same
  config rendered by `fake` then a real provider collided on one run id and silently
  overwrote. Fix: the manifest gains a `render_backend` field recording the effective
  backend that produced the corpus (schema bumped to 1.2), and `execute_run` refuses
  (`RunCollisionError`) to overwrite a run dir whose existing manifest names a
  different backend; a same-backend rerun stays idempotent.
- **F3 — fake-pin override was silent (the dangerous direction).** A config that
  explicitly pinned `backend = "fake"` overridden by a real `--backend` flag emitted no
  warning. Fix: `_resolve_run_client` now warns on any pure value mismatch
  (`config.model.backend != flag`), dropping the `model_fields_set` gate.
- **F4 — snapshot round-trip warned misleadingly.** With the old `anthropic_api`
  default, a config that named no `[model]` block snapshotted `anthropic_api`, so
  reloading the snapshot under the default `fake` flag warned spuriously. Fix:
  `ModelConfig.backend` now defaults to `fake` (the engine's actual default render
  backend, D31), so a minimal config snapshots `fake` and its round-trip is silent.
  Consequence: the config digest hashes `model.backend`, so the golden run id moved
  `40644d551158 → 6c66fbef69f8` (content-identical corpus; see docs/GOLDEN_RUN.md).

Resolved in fix round B:

- **F2 — 1P model id sent to Bedrock.** The default model is a 1P id, which Bedrock
  can't address; it must be given the inference-profile form. Decision: do **not** ship a
  1P→Bedrock id mapping (dated inference-profile ids can't be verified offline, so a wrong
  table is worse than none). Instead **fail fast**: `looks_like_bedrock_model_id` (sharing
  `normalize_model_id`'s regex family) gates `LLMClient.from_config` — the one choke point
  every `build_client` caller (run/eval/bench/reconstruct) shares — and raises a
  `ValueError` naming `[model].name`/`--model` and the `us.anthropic.claude-<family>-
  <YYYYMMDD>-v1:0` shape whenever a `bedrock` client is built with a non-Bedrock model id.
  It fires before any call (dry-run included) and only for `bedrock`.
- **F5 — pricing fallback under-enforces the D13 ceiling.** A model that resolves to no
  pricing row (a fresh 1P id, or a Bedrock family — e.g. opus behind a custom app-
  inference-profile ARN — not in the table) was billed at the sonnet fallback rate
  silently, under-pricing the ceiling ~5× for opus. `pricing_for` now emits a one-time-
  per-model `warnings.warn` when it falls back, so the degradation is visible; still
  deterministic and non-fatal.
- **F6 — stub tests don't pin the real SDK signature.** `scripts/import_smoke.py` now
  `inspect.signature(anthropic.resources.messages.Messages.create)`s the installed SDK and
  fails (naming the vanished kwarg) if it stops accepting any of the seven kwargs the
  backends pass, so a real signature drift breaks CI instead of the first live call.
- **F7 — CLI backend choices hardcoded 6×.** `cli.py` now has one `_BACKEND_CHOICES`
  constant used at all six `--backend` argparse sites; `test_backend_enum_matches_backend_factory`
  asserts it equals the `LLMBackend` values, so the "must match" comment is enforced.

Resolved in fix round C (post-fix adversary re-verify —
`fix(cli): --model overrides for bedrock paths, pre-render collision guard, clean errors`):

- **C1 — `--model` missing on the flagless bedrock paths.** `run`, `eval --judge`, and
  `bench run --runner rag` had no `--model` flag, so a Bedrock inference-profile id could
  only be set via `[model].name` (and not at all for `eval`/`bench`). Fix: `run --model`
  (overrides `[model].name` on the config, so it also flows into the dry-run estimate),
  `eval --judge --model`, and a `bench run --model` shared with the graph runner and now
  honored by the RAG answer step. Each threads into the `LLMConfig`/config the command
  builds, so `--backend bedrock --model <profile-id>` reaches client construction.
- **C2 — collision guard ran after the render (LLM spend).** `execute_run` called
  `_guard_backend_collision` only after `build_corpus`, so a backend collision was detected
  *after* paying for the render. Fix: the guard now runs as soon as the run id/run dir are
  known, before any world build or render; a spy-client test asserts `generate` is never
  called when the guard trips.
- **C3 — F2 `ValueError` / `RunCollisionError` escaped as tracebacks.** `_cmd_run`,
  `_run_rag_runner`, `_cmd_reconstruct_build`, and `eval --judge` caught
  `CostCeilingExceeded`/`RuntimeError` but let the fail-fast `ValueError` and
  `RunCollisionError` crash out. Fix: each now catches those expected types at the same
  boundary and presents a one-line `enterprise-sim <cmd>: <msg>` with a non-zero exit
  (no blanket `ValueError` catch from deep code).
- **C4 — F2 message named a flag that didn't exist everywhere.** With C1 done, the
  "set `[model].name` (or the `--model` flag)" text is now truthful on every path that can
  build a `bedrock` client, so the message stands unchanged.
- **C5 — README quickstart wasn't copy-pasteable.** The Bedrock quickstart now uses the
  new `run --model` flag with an env-var placeholder id, and states the id is account/
  region-specific (runnable verbatim once substituted); `docs/DEVELOPMENT.md`'s entry-point
  list is updated to show `--model` on `run`/`eval --judge`/`bench rag`/`reconstruct build`.

## Open questions (resolved)

- One `bench` extra vs a separate `bedrock` extra (boto3 is heavy)? **Decided: single
  `bench` extra.** `anthropic[bedrock]` folds into the existing real-LLM runtime extra
  (slice 1); no separate `bedrock` extra, no dependency matrix.
- Should `[model].backend` ever drive `run` implicitly? **Decided: no.** `run` renders with
  `fake` unless an explicit `--backend` flag opts in (slice 4); `[model].backend` only
  informs the mismatch warning, it never selects the render backend, so keyless-by-default
  and determinism (D31) hold.
- `claude_cli` backend on Bedrock (`CLAUDE_CODE_USE_BEDROCK` for the CLI)? **Decided:
  documented only, no plumbing.** The env var is described in `docs/DEVELOPMENT.md`
  (Amazon Bedrock section) as the way to point the `claude_cli` backend at Bedrock; no
  dedicated flag was built.
