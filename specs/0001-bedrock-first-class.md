# 0001 — First-class Amazon Bedrock support

Status: approved
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
   by `pricing.py` lookups; `BedrockBackend` gets a per-backend default model constant
   (Bedrock-form of `DEFAULT_MODEL`). Fully keyless-testable.
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

- [ ] `uv sync --extra bench && uv run python scripts/import_smoke.py` constructs the
      Bedrock backend (fails today).
- [ ] With only AWS creds (no `ANTHROPIC_API_KEY`): `enterprise-sim run examples/demo.toml
      --backend bedrock` produces a corpus; cost accounting shows non-zero, correctly
      priced usage for a Bedrock model id.
- [ ] `bench run --runner rag --backend bedrock` and `reconstruct build --backend bedrock`
      work; graph-agent runner documented Bedrock mode works or has a spec'd follow-up.
- [ ] Keyless gate covers the shared SDK request path (no blanket `pragma: no cover` on
      `_call_tool` / error normalization / usage mapping).
- [ ] README + docs/DEVELOPMENT.md contain a copy-pasteable Bedrock quickstart.
- [ ] `./scripts/gate.sh` green on every commit; no new network in default paths.

## Open questions

- One `bench` extra vs a separate `bedrock` extra (boto3 is heavy)? Lean: fold into
  `bench` — it's already "the real-LLM runtime" and one extra is simpler than a matrix.
- Should `[model].backend` ever drive `run` implicitly? Lean: no — explicit `--backend`
  only, keep keyless-by-default; revisit if config-driven automation needs it.
- `claude_cli` backend on Bedrock (`CLAUDE_CODE_USE_BEDROCK` for the CLI) — free win or
  scope creep? Lean: document the env var, don't build plumbing.
