# 0002 — Local testing hardening: backend contract + record/replay

Status: approved
Epic: ROADMAP E2
Owner: unclaimed

## Why

The real-LLM paths (reconstruct extract/resolve, the RAG answer step) are the product's
proof loop, but their behavior against *realistic model output* is only exercised by
keyed tests that skip in CI (`tests/test_reconstruct_extract.py:444-447`,
`tests/test_reconstruct_resolve.py:395-397`, `tests/test_benchmark_rag.py:395-398`). A
regression in parsing, prompt assembly, or answer-resolution against real-shaped output
is invisible until someone pays for a live run. Separately, nothing in the gate reports
coverage, so untested-but-live code is invisible rather than measured.

Constraints: D31 (deterministic, offline default paths; on-disk response cache keyed by
`(prompt_hash, model)`), D10 (determinism is structural), D13 (cost ceiling on any keyed
path), D33 (toolchain), ARCHITECTURE.md §7/§16.3–16.4 (one `LLMClient` over every
backend; two generation modes). The keyless gate must never require the `bench` extra.

## Audit: what spec 0001 already delivered (do not re-propose)

The ROADMAP E2 text predates spec 0001 and is stale in three places:

1. *"today the entire `_AnthropicSDKBackend` request path is `pragma: no cover`"* —
   **stale**. `tests/test_llm_sdk_path.py` (0001 slice 5) covers `_call_tool` request
   shape, both envelope parsers, `_usage_from_sdk`, and `_normalize_sdk_error` with
   stubbed clients, parametrized over `AnthropicAPIBackend` and `BedrockBackend`
   (`tests/test_llm_sdk_path.py:88-89, 131-164`). Remaining pragmas in
   `enterprise_sim/core/llm/backends.py` are the genuinely SDK/CLI-requiring seams:
   `_make_client` (`backends.py:215, 322, 351`) and the `claude_cli` subprocess + parse
   path (`backends.py:380, 409, 430, 448, 456`).
2. *"keep config, CLI choices, and `build_backend` in lockstep with a test"* — **done**.
   `tests/test_config.py:82-93` (`test_backend_enum_matches_backend_factory`) pins
   `LLMBackend` ↔ `build_backend` ↔ `cli._BACKEND_CHOICES` (0001 finding F7), and
   `ModelConfig.backend` defaults to `fake` (0001 finding F4).
3. SDK signature drift is already pinned: `scripts/import_smoke.py:77-100` asserts the
   installed `Messages.create` still accepts the seven kwargs the backends pass (F6).

What genuinely remains: (a) record/replay fixtures so realistic model output is replayed
keyless in CI, (b) a backend *contract* suite (protocol semantics across all four
backends — the delta over `test_llm_sdk_path.py` is real but modest), (c) coverage
visibility in the gate.

## What (scope)

- In scope:
  - Cassette record/replay built on the existing `ResponseCache` (no new recording
    library), covering `extract_chunk`/`adjudicate_pair` and `RagRunner.answer`.
  - A parametrized backend contract suite covering every `LLMBackend` value, with a
    completeness pin so a future backend must join it.
  - Coverage reporting wired into `scripts/gate.sh` (report-only first, then a floor).
  - `make` targets and `docs/DEVELOPMENT.md` documentation for the record procedure.
- Out of scope (explicitly):
  - Mutation testing (not in E2).
  - HTTP-level recording (vcrpy/respx) — rejected, see Design.
  - Score-regression baselines and e2e eval promotion (ROADMAP E3).
  - Coverage upload/badges/Codecov; CI artifact publishing.
  - Any new engine primitive: zero production-code behavior changes. The only
    `enterprise_sim/**` edits permitted are removing `# pragma: no cover` comments that
    the new tests make false.

## Design sketch

### 1. Cassettes = the existing ResponseCache, pointed at a committed directory

**Decision: reuse `ResponseCache` as the cassette mechanism; do not add vcrpy or a new
fixture format.** Rationale:

- Every target path already funnels through `LLMClient` and its cache-first `_call`
  pipeline: extract (`enterprise_sim/reconstruct/extract.py:329`), resolve adjudication
  (`enterprise_sim/reconstruct/resolve.py:236`), RAG answer
  (`enterprise_sim/benchmark/runners/rag.py:468`). A cache hit skips the backend
  entirely (`enterprise_sim/core/llm/client.py:349-354`), so replay is *the production
  code path for a warm cache* — testing it is itself D31 coverage.
- The cache key already pins everything that matters: prompt hash, model, mode, schema,
  candidate set, temperature (`enterprise_sim/core/llm/cache.py:26-55`). If a prompt,
  schema, or model drifts, replay misses loudly instead of silently replaying a stale
  interaction — exactly the drift-detection a cassette should give.
- The serialized artifact is `Completion.to_dict()` — text/usage/model/structured/
  references_used only (`enterprise_sim/core/llm/types.py:81-90`). **No headers, no
  request body, no credentials can be recorded by construction**, unlike HTTP-level
  recording where key redaction is a standing risk. Files are `sort_keys, indent=2`
  JSON (`cache.py:116`), so recordings diff cleanly in review.
- Zero new dependencies; the keyless gate stays `--extra dev` only.

What HTTP recording would buy that this doesn't: coverage of `_call_tool`'s request
assembly against a live wire format. That is already covered by the stubbed-client tests
(`tests/test_llm_sdk_path.py`) plus the import-smoke signature pin — cassettes are
complementary (realistic *response content* through the parsing/resolution pipeline),
not a transport test. State this in the test module docstring so nobody mistakes replay
for transport coverage.

**Mechanics.** New test-support module `tests/llm_stubs.py` (importable by test modules;
pytest's default import mode puts `tests/` on `sys.path`):

- `CassetteMissBackend` — implements the `Backend` protocol; *any* call raises
  `LLMError` (terminal, so the client's retry loop does not spin —
  `client.py:369-377`) with a message naming the cache key, the likely cause
  ("prompt/schema/model/temperature drifted from the recording"), and the re-record
  command. Lives in tests, not core: the protocol is duck-typed
  (`backends.py:38-69`), so no `enterprise_sim/**` change is needed.
- `cassette_client(dir: Path) -> LLMClient` — `LLMClient(CassetteMissBackend(),
  config=LLMConfig(backend="cassette-replay", model=HAIKU_MODEL, cache_dir=str(dir)))`.
  The direct constructor bypasses `build_backend`, so no registry change.
- Record mode: the same tests, driven by an env var. When `ESIM_CASSETTES=record`, the
  client fixture instead returns `build_client(LLMConfig(backend="anthropic_api",
  model=HAIKU_MODEL, cache_dir=str(dir), cost_ceiling_usd=1.0))` — cache misses hit the
  real API and write the cassette files; the assertions run against fresh output. This
  keeps record and replay in one source of truth (no separate record script that can
  drift from the tests), the same pattern as VCR record modes. Requires
  `ANTHROPIC_API_KEY` + `--extra bench`; the fixture skips with a clear message if
  either is missing in record mode.
- Model/temperature pinning: every scenario passes an explicit model
  (`HAIKU_MODEL`, the production extract default — `extract.py:69`) and relies on the
  shared `LLMConfig` defaults for temperatures, so record and replay compute identical
  `request_key`s.

**Scenario inputs are frozen literals, never live-generated fixtures.** The extraction
and adjudication scenarios use committed literal chunk texts / mention pairs (in the
style of `_ORG_TEXT`/`_GOAL_TEXT` in `tests/test_reconstruct_extract.py`). The RAG
scenario builds a `RagRunner` directly from a handful of literal `Chunk`s
(`BM25Index.build`) and a literal alias mapping (`AliasResolver.of`) — *not* from the
session golden-run fixture (`tests/test_benchmark_rag.py:38-50`). This is the load-
bearing decoupling: **a golden-run pin change can never invalidate a cassette**, so a
keyless contributor who regenerates the pin is never blocked behind a keyed re-record.

**Layout and lifecycle.** Cassettes live in `tests/cassettes/<scenario>/` (e.g.
`extract/`, `resolve/`, `rag/`) as `<sha256>.json` files, committed to the repo.
Semantics, per scenario directory:

- Directory **absent** → the replay tests `pytest.skip` with the record command. This is
  the state after the implementation lands but before the (keyed) owner records; the
  keyless gate stays green either way.
- Directory **present** → strict replay: a cache miss fails the test via
  `CassetteMissBackend` (no silent skip, no network).
- **Re-record procedure** (documented in `docs/DEVELOPMENT.md`): delete the scenario
  directory (stale hits would otherwise short-circuit recording — `client.py:350-354`),
  then `ESIM_CASSETTES=record uv run pytest tests/test_llm_cassettes.py` with a key and
  `--extra bench`, then re-run `./scripts/gate.sh` keyless (unset the key or rely on
  replay determinism) and commit the new JSON. `make record-cassettes` wraps this.
  Escape hatch for a keyless contributor who *must* change a recorded prompt: move the
  scenario directory aside in the same PR (tests flip to skip, visibly) and flag the
  re-record as a follow-up — a reviewable, loud act rather than a broken gate.
- **Redaction belt-and-braces**: although `Completion.to_dict()` cannot contain
  credentials, record mode scans every written file for the live
  `ANTHROPIC_API_KEY` value and the `sk-ant-` prefix and fails the recording if found.

Assertion style: structural invariants that any competent model satisfies on these tiny
fixtures (ontology-valid types, located spans, the obviously-correct merge/answer),
same spirit as the existing keyed tests (`test_reconstruct_extract.py:450-473`).
Re-recording may legitimately require loosening a content assertion; that is normal
cassette maintenance and is documented.

### 2. Backend contract suite — refactor-and-extend, honestly scoped

`tests/test_llm_sdk_path.py` already proves the *SDK pair* shares one request path. The
genuine delta is a suite asserting **Backend-protocol semantics for every backend**, so
a future `vertex`/openai-compat backend has a checklist to pass:

- New `tests/test_backend_contract.py`, parametrized over factories for all four
  `LLMBackend` values: `fake` (real, no stub), `anthropic_api`/`bedrock` (stub client
  injected — move `_StubClient`/`_StubMessages`/`_tool_use_response` from
  `test_llm_sdk_path.py` into `tests/llm_stubs.py` and import from both files),
  `claude_cli` (monkeypatch `_run` to return canned CLI JSON — the seam at
  `backends.py:380`; `_run` itself keeps its pragma, but the parse path
  `generate_structured`/`generate_content`/`_extract_json_object`
  (`backends.py:402-473`) loses its pragmas because the contract suite now executes it).
- Contract assertions per backend, both modes: returns a `Completion`; `structured` is a
  dict honoring the schema's `required` and `enum` constraints (hand-rolled minimal
  checker, no jsonschema dep); `text` is the sorted-key JSON of `structured` in
  structured mode; `references_used` is a `tuple[str, ...]`; `usage` fields are
  non-negative ints; `model` is echoed; provider failures surface as `LLMError`
  subclasses (injectable for the SDK/CLI backends).
- **Completeness pin**: assert the parametrized backend names equal
  `{b.value for b in LLMBackend}` — adding a backend to the enum without adding it to
  the contract suite fails the gate (same enforcement pattern as
  `test_config.py:82-93`).
- `test_llm_sdk_path.py` keeps the SDK-specific request-shape assertions
  (cache_control layering, forced tool-use) — those are not protocol contract, they are
  SDK-path implementation pins.

### 3. Coverage visibility in the gate

**Decision: `pytest-cov` (coverage.py), config in `pyproject.toml`, flags only in
`scripts/gate.sh`** — a bare `uv run pytest` (the fast iteration loop,
`pyproject.toml:87-90` addopts untouched) stays coverage-free.

- `pyproject.toml`: add `pytest-cov>=5` (and thereby `coverage>=7.4`) to the `dev`
  extra (`pyproject.toml:36-43`); add `[tool.coverage.run] source = ["enterprise_sim"]`
  (statement coverage only — branch coverage is unsupported by the fast `sysmon` core
  and is a later tightening) and `[tool.coverage.report]` (`show_missing` off by
  default; precision 1). `.coverage` goes in `.gitignore`.
- `scripts/gate.sh` (both modes — CI visibility is the point, so `--check` does not
  differ): the pytest step becomes
  `COVERAGE_CORE=sysmon uv run pytest --cov --cov-report=` (empty report suppresses the
  per-file terminal table — no CI log spam), followed by one summary line built from
  `uv run coverage report --format=total`, e.g.
  `coverage: 87.3% total (run 'make coverage' for per-file detail)`.
  `COVERAGE_CORE=sysmon` uses Python 3.12's `sys.monitoring` core for near-zero
  overhead; if measurement shows the default core already meets the timing budget,
  keeping it is fine — the acceptance criterion is the budget, not the core.
- `make coverage`: `uv run coverage report --show-missing --skip-covered` for the
  human/agent view.
- **Floor (separate slice, after a baseline is observed)**: set
  `fail_under = <measured total, rounded down to an integer, minus 1>` in
  `[tool.coverage.report]`; the gate's `coverage report` call then exits non-zero on
  regression. One point of slack absorbs noise from refactors; keyed environments can
  only *raise* coverage (keyed tests are `pragma: no cover` and additive), so the floor
  never flakes keyless-vs-keyed.

### 4. Small extras the epic needs

- `make record-cassettes` (keyed, manual) and `make coverage` targets in the thin-
  wrapper `Makefile` (`Makefile:1-3` philosophy: gate.sh stays the source of truth).
- `docs/DEVELOPMENT.md`: a "Cassette record/replay" section (procedure, drift failure
  modes, escape hatch) and a "Coverage" note.
- ROADMAP E2 entry updated to point here and correct the stale bullets (done alongside
  this spec).
- Explicitly **no** keyed-live `make` target beyond `record-cassettes`: keyed tests
  already self-activate when `ANTHROPIC_API_KEY` is set (skipif-based, no marker
  plumbing to add), and a scheduled keyed CI belongs to ROADMAP E3.

### Slices (each independently gate-green)

| # | Slice | Commit type | Proof |
|---|---|---|---|
| 1 | Extract shared stubs to `tests/llm_stubs.py`; add `tests/test_backend_contract.py` over all four backends with the completeness pin; drop now-false pragmas on the `claude_cli` parse path | `test(llm)` | `uv run pytest tests/test_backend_contract.py tests/test_llm_sdk_path.py` |
| 2 | Cassette infrastructure: `CassetteMissBackend`, `cassette_client`, `ESIM_CASSETTES=record` fixture mode, redaction scan, skip-if-unrecorded semantics, plus a keyless round-trip self-test (record with the `fake` backend into `tmp_path`, replay via `CassetteMissBackend`, assert byte-identical results and that a mutated prompt raises the miss error) | `feat(tests)` | `uv run pytest tests/test_llm_cassettes.py` (self-test green; scenario tests skip) |
| 3 | Frozen scenarios + replay tests for `extract_chunk` and `adjudicate_pair`; owner records and commits `tests/cassettes/{extract,resolve}/` | `feat(tests)` | keyless: tests skip pre-recording / replay post-recording; keyed: `make record-cassettes` |
| 4 | Frozen mini-corpus scenario + replay test for `RagRunner.answer` (retrieve → answer → resolve end-to-end); owner records `tests/cassettes/rag/` | `feat(tests)` | same as slice 3 |
| 5 | Coverage report-only: `pytest-cov` in `dev`, `[tool.coverage.*]` in pyproject, gate.sh one-line summary, `make coverage`, `.gitignore` | `feat(gate)` | `./scripts/gate.sh` prints a coverage line; timing budget holds |
| 6 | Coverage floor: `fail_under` = observed baseline (integer) − 1 | `feat(gate)` | `./scripts/gate.sh --check` fails when coverage drops below the floor |
| 7 | Docs: `docs/DEVELOPMENT.md` sections, Makefile targets finalized, spec status header | `docs` | review |

Slice 5/6 are independent of 1–4 and may land in either order. Slices 3–4 have a keyed
recording half; the keyless halves (scenarios + skip semantics) land regardless, and the
spec's acceptance marks the recording as pending-key if no key is available at
implementation time (same convention as spec 0001's live-validation items).

### Invariant impact

- **Keyless gate**: no new gate dependency beyond `pytest-cov` (pure-Python dev tool,
  no stubs needed — it's a plugin, never imported by typed code). Replay reads committed
  JSON only; `CassetteMissBackend` imports nothing from `bench`. Record mode is env-var
  + key + `--extra bench` gated and never runs in CI. `scripts/import_smoke.py`
  unchanged (no new lazy imports anywhere).
- **Determinism (D10/D31)**: replay is byte-stable file reads through the production
  cache path; no network, wall-clock, or randomness added to any default path. The only
  nondeterministic act (recording) is manual and keyed.
- **Core purity / extensibility (D2/D24)**: zero behavior changes under
  `enterprise_sim/`; the only production-tree edit is deleting `# pragma: no cover`
  comments made false by slice 1 (precedent: 0001 slice 5). Everything else is
  `tests/`, `scripts/gate.sh`, `Makefile`, `pyproject.toml`, docs.
- **Golden-run pin**: unaffected. Cassette scenarios use frozen literal inputs by
  design, so regenerating the golden pin neither reads nor invalidates
  `tests/cassettes/**`; conversely cassettes are never consulted by
  `tests/test_golden_run.py` or any run/render path (the cassette `cache_dir` exists
  only inside these test fixtures). No `docs/GOLDEN_RUN.md` change.
- **Gate speed**: budget is ≤1.5× on the pytest step (see acceptance); expected actual
  overhead with the sysmon core is a few percent on this ~20s suite.
- **Cost (D13)**: record mode sets `cost_ceiling_usd=1.0` on its client, so a runaway
  recording cannot spend more than $1.

## Test & validation plan

- Keyless (in `./scripts/gate.sh`): the contract suite (all four backends, stubbed);
  the cassette round-trip self-test (fake-backend record → replay → byte-equality, and
  drift → `LLMError` naming the re-record command); scenario replay tests once
  cassettes are committed (strict-miss behavior covered by the self-test); a test that
  scenario tests *skip* when the cassette dir is absent (point the fixture at an empty
  tmp dir); coverage config smoke (gate script emits the summary line — validated by
  running the gate).
- Keyed/manual: `make record-cassettes` (needs `ANTHROPIC_API_KEY` + `--extra bench`;
  ceiling $1) — produces/refreshes `tests/cassettes/**`; the redaction scan runs during
  recording. No new keyed tests beyond the recording fixture itself.
- Golden run pin: no change (see invariant impact); no regen needed.

## Acceptance criteria

- [ ] `uv run pytest tests/test_backend_contract.py` passes keyless and its
      parametrization provably covers every `LLMBackend` value (the completeness
      assertion fails if a fifth backend is added without joining the suite).
- [ ] `rg "pragma: no cover" enterprise_sim/core/llm/backends.py` no longer matches the
      `claude_cli` parse functions (`generate_structured`, `generate_content`,
      `_extract_json_object`); only the genuinely SDK/CLI-requiring seams
      (`_make_client` ×3, `_run`, `_estimated_usage`) remain.
- [ ] `uv run pytest tests/test_llm_cassettes.py` passes keyless, offline, with
      `--extra dev` only: self-test green; scenario tests replay green (or skip with
      the documented message if the owner has not yet recorded — recording is then a
      tracked pending-key item, as in spec 0001).
- [ ] With cassettes present, running the scenario tests with the network unavailable
      and no `ANTHROPIC_API_KEY` succeeds twice in a row with identical results
      (byte-stable replay, D31).
- [ ] Mutating a recorded scenario's prompt text locally makes its replay test fail
      with a message containing the re-record command (demonstrable via the round-trip
      self-test's drift case).
- [ ] `grep -c sk-ant tests/cassettes -r` finds nothing in committed cassettes (vacuous
      until recorded; enforced at record time by the redaction scan).
- [ ] `ESIM_CASSETTES=record uv run pytest tests/test_llm_cassettes.py` without a key
      skips (does not error) with a message naming the key and extra required.
- [ ] `./scripts/gate.sh` prints exactly one coverage summary line (no per-file table
      in CI logs) and `make coverage` prints the per-file view.
- [ ] Timing budget: `time uv run pytest -q` vs
      `time env COVERAGE_CORE=sysmon uv run pytest -q --cov --cov-report=` — the
      covered run's wall time is ≤1.5× the uncovered run on the same machine.
- [ ] After slice 6: lowering `fail_under` proof — temporarily deleting a well-covered
      test file and running `./scripts/gate.sh --check` exits non-zero at the coverage
      step (restore afterward); with the tree intact the gate is green.
- [ ] `make help` lists `coverage` and `record-cassettes`; `docs/DEVELOPMENT.md`
      documents the record/re-record procedure, the drift failure mode, and the
      keyless escape hatch (move the scenario dir aside in the same PR).
- [ ] `specs/ROADMAP.md` E2 entry references this spec and no longer claims the SDK
      request path is uncovered.
- [ ] `./scripts/gate.sh` green on every commit; no network in any default path.

## Open questions

- **Should replay strictness be a core `ResponseCache` mode instead of a test-side
  backend?** Lean: no — a tests-only `CassetteMissBackend` (~20 lines) gets identical
  behavior through the duck-typed `Backend` protocol without touching
  `enterprise_sim/core/**`. Revisit only if a non-test consumer (e.g. E5's cache
  hit-rate work) wants a strict mode.
- **Human-readable cassette index (scenario label → key files)?** Lean: skip. The JSON
  files are sorted-key, indented, and contain the model + structured payload, which is
  enough for review; an index would need `request_key` interception for marginal value.
- **Branch coverage / raising the floor over time?** Lean: statement coverage +
  baseline−1 now; tighten only when a concrete gap shows up. Branch coverage forfeits
  the sysmon fast path today.
- **Record against `bedrock` too?** Lean: no — the cassette content is provider-
  agnostic (`Completion` has no provider fields) and `request_key` folds only the model
  string; one 1P recording per scenario suffices. A Bedrock-recorded variant adds cost
  and churn for no new assertion.
- **Content-assertion tightness on recorded output.** Lean: match the existing keyed
  tests' structural style plus the few obviously-stable content checks; accept that a
  re-record may adjust them. If re-records prove churny, downgrade to structural-only.
