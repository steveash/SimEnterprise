# Explorer: creating, extending, and driving runs

> Part of [`EXPLORER.md`](./EXPLORER.md). This is the design + user guide for the
> **Runs** view: start a run from a form instead of a TOML file, extend a finished
> run with more scenario instances or more time, watch progress / ETA / cost while
> it runs, and pause + resume it from a snapshot of its progress.

---

## 1. User stories

1. **New run.** Fill in a company, a time window, the departments and scenario
   instances I want, the model + cost knobs, click *Estimate* to see the artifact
   count and projected cost, then *Start*. Everything `enterprise-sim run` accepts
   (config fields plus `--live`, `--max-concurrency`, `--cost-ceiling`) is on the form.
2. **Extend a run.** From a finished run, add more time (later `period_end`)
   and/or more scenario instances (anchor projects, optionally bound to a chosen
   playbook + department, including template ones) and produce a child run that
   records its lineage. Work already rendered by the parent is reused, not re-billed.
3. **Live progress.** While the job runs I see the phase, artifacts done / total,
   elapsed, **estimated time remaining**, **cost so far**, and **projected total
   cost** (spent + remaining), updating per rendered artifact.
4. **Pause / resume.** A long run can be paused; its progress is snapshotted so
   resuming later (even after the app or machine restarted) continues from where it
   stopped, paying only for work not yet done. Cancel discards the job.

## 2. Why "snapshot of progress" = the response cache + a job ledger

The pipeline (`ARCHITECTURE.md` §6) is deterministic up to the LLM: Layer A
(world) and Layer B (schedule) are pure functions of `(config, seed)` and take
well under a second; **all the time and money is in Layer C**, one model call per
`(deliverable event × producer)`. The repo already has decision **D31**: every
completed call is written atomically to the on-disk `ResponseCache`
(`enterprise_sim/core/llm/cache.py`), keyed by the full prompt + model + backend.

So the cheapest *correct* snapshot is: **make the cache mandatory and per-job**,
and record cumulative accounting in a job ledger. Resuming a paused/killed job
re-runs the same config; Layers A/B replay identically, and every artifact that
was already rendered comes back as a cache hit (free, milliseconds). Only the
calls that never completed are issued. This is exactly the resume story
`examples/haiku_quarter.toml` already documents, made first-class:

- no new serialization format for `World`/journals/artifacts (nothing to drift),
- correctness by construction (a resumed run is byte-identical to an
  uninterrupted one on any backend, because the cache replays the same completions),
- pause is *cooperative* (finish the in-flight artifact, then stop) so no money
  is wasted, but a hard kill is equally safe (an incomplete call is simply not cached).

The ledger (`state.json`) carries what the cache cannot: cost spent across
segments (cache hits are priced $0 by `CostTracker`, so the prior segments'
spend must be summed from the ledger), the artifact counts, and timings.

## 3. Python: the `enterprise_sim.jobs` package

### 3.1 Progress events (`jobs/progress.py`)

A thread-safe sink the pipeline reports into. One JSON object per event; the
same objects go to `stdout` (for the sidecar) and to `<job>/progress.jsonl`.

```python
class ProgressSink(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...

@dataclass(frozen=True, slots=True)
class ProgressEvent:
    kind: str            # see table
    ts: float            # wall clock, time.time()
    data: dict[str, Any] # kind-specific payload
```

| `kind` | when | `data` |
|--------|------|--------|
| `phase` | entering a phase | `{"phase": "world" \| "schedule" \| "estimate" \| "render" \| "assemble" \| "evals" \| "done"}` |
| `world_built` | after Layer A | `{"nodes", "edges", "departments", "scenarios"}` |
| `scheduled` | after Layer B | `{"events", "artifacts_total", "scenarios": [{"id", "artifacts"}]}` |
| `estimate` | after the D13 gate | `{"artifacts_total", "estimated_cost_usd", "model", "input_tokens_each", "output_tokens_each"}` |
| `artifact` | after each rendered artifact | `{"scenario_id", "event_id", "producer", "path", "cached": bool, "done", "total", "cost_usd_segment", "calls", "cache_hits", "usage": TokenUsage.to_dict()}` |
| `paused` | cooperative pause honoured | `{"done", "total"}` |
| `error` | the job failed | `{"message", "type"}` (e.g. `CostCeilingExceeded`) |
| `done` | run written | `{"run_id", "run_dir", "artifacts", "events", "cost_usd_segment", "cost_usd_total"}` |

`execute_run(config, *, progress: ProgressSink | None = None, control: RunControl | None = None, ...)`
and `build_corpus(...)` gain these two keyword-only parameters (default `None`
keeps every existing caller and test byte-identical). `_render_scenario` emits
`artifact` after `apply_to_world(world, produced)` for each event, reading
`client.cost` for the running totals (that property is a live, locked
`CostTracker`; a call that was a cache hit reports `cached=True` from
`ContentResult.cache_hit` on any produced artifact of that event).

### 3.2 Cooperative pause (`jobs/control.py`)

```python
class RunControl:
    def __init__(self, job_dir: Path): ...
    def request_pause(self) -> None          # writes control.json {"pause": true}
    def should_pause(self) -> bool           # reads control.json (cheap, cached mtime) or a SIGTERM flag
class RunPaused(Exception): ...
```

`_render_scenario` calls `control.should_pause()` **before** starting each
event's render and raises `RunPaused` if set. Because renders fan out over a
`ThreadPoolExecutor`, the first `RunPaused` propagates out of `generate_many`
while sibling threads finish their current artifact (each is cached on
completion) and then also stop at their next check. The worker catches
`RunPaused`, emits `paused`, writes `state.json{"status":"paused"}` and exits `0`.
`SIGTERM`/`SIGINT` set the same flag (graceful), so closing the app pauses
rather than corrupts.

### 3.3 Job directory and ledger (`jobs/state.py`)

`runs/.jobs/<job-id>/` where `job-id = <yyyymmdd-hhmmss>-<slug>` (creation time
+ company slug; unique, sortable, human-readable).

| file | who writes | contents |
|------|-----------|----------|
| `job.json` | UI (via `job create`) | the immutable request: `{"kind": "new"\|"extend", "live": bool, "config": RunConfig JSON, "parent_run_dir": str\|null, "changes": {...}\|null, "created_at"}` |
| `config.json` | `job create` | the effective `RunConfig` (JSON; `load_config` already accepts `.json`), with `scale.cache_dir` forced to `<job>/llm-cache` unless the config set one (an extend job inherits the parent's cache dir, §3.5) |
| `state.json` | worker, atomically (`tmp` + `replace`) after every event | `{"status": "created"\|"running"\|"paused"\|"failed"\|"done"\|"cancelled", "pid", "run_id", "run_dir", "phase", "artifacts_done", "artifacts_total", "artifacts_cached", "estimate": {...}, "cost_usd_total", "cost_usd_segment", "usage_total", "segments": [{"started_at", "ended_at", "artifacts_rendered", "cost_usd", "reason": "paused"\|"done"\|"failed"\|"killed"}], "started_at", "updated_at", "finished_at", "error"}` |
| `progress.jsonl` | worker (append) | every `ProgressEvent`, so the UI can rebuild the timeline after a restart |
| `control.json` | UI / worker | `{"pause": true}` request flag; removed on resume |
| `llm-cache/` | `ResponseCache` | the snapshot itself |
| `worker.log` | worker | stderr of the run (tracebacks) |

`state.json` is the single source of truth the UI lists; a job whose `status`
is `running` but whose `pid` is dead is shown as **interrupted** (resumable).

### 3.4 The worker (`jobs/worker.py`) and CLI (`jobs/cli.py`)

```
enterprise-sim job catalog                      # JSON: archetypes, playbooks (incl. templates), producers,
                                                #       backends, models + pricing, company sizes, config defaults
enterprise-sim job estimate --config CFG.json   # JSON RenderEstimate + world/schedule counts (dry run, keyless)
enterprise-sim job create  --jobs-root DIR --config CFG.json [--live]
                           [--extend RUN_DIR]   # writes job.json/config.json/state.json; prints {"job_id","job_dir"}
enterprise-sim job run     JOB_DIR              # runs or RESUMES the job; streams progress JSONL on stdout
enterprise-sim job pause   JOB_DIR              # writes control.json; the running worker stops cooperatively
enterprise-sim job status  JOB_DIR              # prints state.json
enterprise-sim job list    --jobs-root DIR      # prints every state.json (+ job.json summary)
```

`job run` is idempotent: it loads `config.json`, opens a new **segment** in
`state.json`, builds an `LLMClient` from the config (`live` from `job.json`),
runs `execute_run(config, progress=..., control=...)`, then **finalizes**:
writes `lineage.json` for extend jobs, generates the run's eval question set
(`enterprise-sim evals generate`, cheap and deterministic — see
`EXPLORER_EVALS.md`), and marks the state `done`. On `RunPaused` → `paused`; on
`CostCeilingExceeded`/any exception → `failed` with the message (still resumable:
raising the ceiling in the UI edits `config.json` and re-runs).

`cost_usd_total = sum(segment.cost_usd)`; `cost_usd_segment` is
`client.cost.total_cost_usd` of the live segment. Both are in every `artifact`
event so the UI never has to add across files.

### 3.5 Extending a run (`jobs/extend.py`)

`extend_config(parent: RunConfig, *, period_end: date | None, add_projects: Sequence[ProjectConfig], departments: Sequence[DepartmentConfig] | None) -> RunConfig`
derives the child config from the parent's `config.snapshot.json`:

- **more time**: `simulation.period_end` may only move later (validated);
- **more instances**: appended `[[projects]]` (each is one more scenario bound
  to a playbook — the parent's are kept verbatim, in order);
- everything else (seed, company, model) is inherited so Layer A reproduces the
  same org. The child's `run_id` differs (the digest covers projects + window),
  so the parent is never overwritten.

**Reuse guarantee (measured, not assumed).** Anchored projects draw from their
own seeded sub-stream (`SeedContext.rng("project", project_id)`) and the
scheduler seeds per `(scenario, activation)`, so:

- **More time only** (later `period_end`, nothing added): the child's world is a
  superset of the parent's (node *and* edge ids), the earlier events' prompts are
  identical, and every parent artifact comes back as a cache hit — the number of
  *non-cached* renders equals `child total − parent total`. Locked by
  `tests/test_jobs_extend.py`.
- **Added scenario instances** (new `[[projects]]`): node ids and structural edges
  (`authored`, `reviewed`, `expresses`, `under`, …) still superset cleanly, but the
  grounding roster every prompt carries (D30 layer 1) lists *all* projects in the
  company, so every prompt changes by one line and the parent's artifacts are
  **re-rendered** (and their LLM-chosen `references` edges may differ). The UI
  therefore shows the estimate for an extension honestly: "more time" is priced at
  the new deliverables only; "more instances" is priced as a full re-render, with
  the cached/new split visible while it runs.

The child job's `llm-cache/` is a **copy-on-start** of the parent's cache dir (the
parent job's `llm-cache/` if the parent was a job, else the parent config's
`scale.cache_dir` if any), so whatever prompts *are* identical are free.

`lineage.json` (in the child run dir): `{"parent_run_id", "parent_run_dir", "changes": {"period_end": [old, new], "added_projects": [...], "departments": [...]}}`.
The Explore view shows a *derived from …* badge and the diff panel pre-selects the parent.

### 3.6 Config additions (`core/config/models.py`, additive, defaults keep old configs valid)

```toml
[[departments]]          # NEW, optional: explicit archetype selection (ordered; first = primary).
archetype = "engineering"   # any registered archetype, including template ones
                            # when omitted, the vertical/size-driven selection applies as today

[[projects]]
name = "Billing migration"
description = "…"
playbook = "build_software"  # NEW, optional: default = the department archetype's first playbook
department = "engineering"   # NEW, optional: default = the primary department

plugins = ["templates"]      # NEW, optional: extra plugin dirs discovered before Layer A (see EXPLORER_TEMPLATES.md)
```

Builder changes: `_select_archetypes` honours `config.departments` when given;
`_build_config_projects` attaches each project under its named department with
its named playbook (validated against the registries with a clear error). The
digest includes both fields (they change *what* the run is).

## 4. Sidecar (`src/sidecar/jobs/`)

`JobManager` (pure supervision, no simulation logic):

| op | params | result |
|----|--------|--------|
| `jobCatalog` | – | `job catalog` JSON (cached for the process lifetime; refreshed by `templatesChanged`) |
| `jobEstimate` | `{config}` | `job estimate` JSON |
| `jobCreate` | `{config, live, extend?: {parentRunDir, changes}}` | `{jobId, jobDir}` |
| `jobStart` | `{jobDir}` | spawns `job run`; `{pid}`; also used for **resume** |
| `jobPause` | `{jobDir}` | `job pause`; the manager also sends `SIGTERM` if no progress within 60 s (belt and braces) |
| `jobCancel` | `{jobDir}` | pause then mark `cancelled` in `state.json`; the job dir is kept (the user can delete it) |
| `jobDelete` | `{jobDir}` | removes the job dir (not the run dir) |
| `jobList` | – | every `state.json` under `runs/.jobs`, plus `alive` (pid check) |
| `jobStatus` | `{jobDir}` | `state.json` |
| `watchJob` | `{jobDir}` | stream: replays `progress.jsonl` (so a reattached client rebuilds the view), then live events until `unwatchJob` |
| `unwatchJob` | `{id}` | – |
| `readRunConfig` | `{runPath}` | the run's `config.snapshot.json` (+ `lineage.json` if any) — feeds the Extend form |

The manager tails the worker's stdout into per-job subscriber sets; when the
process exits it reads `state.json` and emits a final `state` event.
`artifact` events carry both `cost_usd_segment` (this process) and
`cost_usd_total` (all segments; the worker adds the closed segments' spend).
**Estimates are computed in the renderer** from the event stream (`src/renderer/jobs/projection.ts`, unit-tested):

- `remainingSeconds = (total - done) × meanSecondsPerNonCachedArtifact` (EWMA over the current segment; cached artifacts count as 0 s);
- `projectedTotalCost = costSoFar + (total - done) × meanCostPerNonCachedArtifact`, falling back to the D13 per-artifact estimate until three real artifacts have been observed;
- render throughput accounts for concurrency implicitly (wall-clock per artifact as observed).

## 5. Renderer: the Runs view

- **Job list** (left): every job with status pill (running / paused / interrupted / failed / done / cancelled), progress bar, cost. Actions: *Pause*, *Resume*, *Cancel*, *Delete*, *Open in Explore* (loads `run_dir` and switches view).
- **New run** (form, not a wizard — one scrollable page with sections mirroring the TOML): Company · Window & seed · Departments (multi-select from catalog, ordered) · Scenario instances (project rows: name, description, playbook, department) · Model (backend, model, realism, `live` switch with a clear "costs money" note) · Scale (concurrency, cost ceiling, cache dir, per-artifact token estimates) · Output dir. *Estimate* runs `jobEstimate` and shows artifacts/events/cost; *Start* runs `jobCreate` + `jobStart`. *Load from TOML…* pre-fills from any example config (`job catalog` returns the defaults; a `job config-from-toml` helper converts).
- **Extend run**: opened from a finished run (job list or Explore topbar). Pre-filled from `readRunConfig`; only `period_end`, new project rows, and the model/scale knobs are editable; shows what will change and the *Estimate* delta (child estimate − artifacts expected to be cache hits).
- **Job detail**: phase timeline, `done/total` with cached/new split, elapsed, ETA, cost so far / projected total, model + backend, live log tail (last 200 progress lines), error banner with *Raise ceiling & resume* when the failure is `CostCeilingExceeded`.

## 6. Tests

Python (`tests/test_jobs_*.py`): progress events on the golden config (counts
match the manifest); pause honoured mid-render on the fake backend (a control
file written from a sink callback after artifact N → `paused` state with N
artifacts, resume → `done`, run byte-identical to an uninterrupted run);
ledger totals across two segments; extend guarantees (§3.5); config additions +
digest; CLI JSON round-trips.
Sidecar (`tests/jobs.test.ts`): projection math; JSONL tailing + reattach
replay; a spawned golden job end-to-end (skips when Python is unavailable).
