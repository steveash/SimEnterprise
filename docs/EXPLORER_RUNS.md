# Explorer: creating, extending, and driving worlds

> Part of [`EXPLORER.md`](./EXPLORER.md). This is the design + user guide for the
> **Worlds** view: every world on disk in one list; create a world from a form
> instead of a TOML file, extend a world **in place** with more scenario instances
> or more time, watch progress / ETA / cost while it builds, and pause + resume it
> from a snapshot of its progress. A world is one folder with one `.world` file
> (`enterprise_sim.worlds`); the CLI (`enterprise-sim world …`) and the UI drive
> the same thing, so a world built from the command line shows up here too.

---

## 1. User stories

1. **New run.** Fill in a company, a time window, the departments and scenario
   instances I want, the model + cost knobs, click *Estimate* to see the artifact
   count and projected cost, then *Start*. Everything `enterprise-sim world create` accepts
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

So the cheapest *correct* snapshot is: **make the cache mandatory and world-local**
(`<world>/.cache/llm`), and record cumulative accounting in a job ledger inside the
`.world`. Resuming a paused/killed job
re-runs the same config; Layers A/B replay identically, and every artifact that
was already rendered comes back as a cache hit (free, milliseconds). Only the
calls that never completed are issued. This is exactly the resume story
`examples/haiku_quarter.toml` already documents, made first-class:

- no new serialization format for `World`/journals/artifacts (nothing to drift),
- correctness by construction (a resumed run is byte-identical to an
  uninterrupted one on any backend, because the cache replays the same completions),
- pause is *cooperative* (finish the in-flight artifact, then stop) so no money
  is wasted, but a hard kill is equally safe (an incomplete call is simply not cached).

The ledger (the `.world`'s `job` block) carries what the cache cannot: cost spent across
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
| `phase` | entering a phase | `{"phase": "world" \| "schedule" \| "estimate" \| "render" \| "assemble" \| "data" \| "evals" \| "done"}` |
| `data_scenario` | a structured-data scenario starts (only when `[data]` lists any) | `{"scenario", "index", "total"}` |
| `data_done` | a data scenario finished | `{"scenario", "rows", "tables", "questions_total", "questions_answerable"}` |
| `world_built` | after Layer A | `{"nodes", "edges", "departments", "scenarios"}` |
| `scheduled` | after Layer B | `{"events", "artifacts_total", "scenarios": [{"id", "artifacts"}]}` |
| `estimate` | after the D13 gate | `{"artifacts_total", "estimated_cost_usd", "model", "input_tokens_each", "output_tokens_each"}` |
| `artifact` | after each rendered artifact | `{"scenario_id", "event_id", "producer", "path", "cached": bool, "done", "total", "cost_usd_segment", "calls", "cache_hits", "usage": TokenUsage.to_dict()}` |
| `paused` | cooperative pause honoured | `{"done", "total"}` |
| `error` | the job failed | `{"message", "type"}` (e.g. `CostCeilingExceeded`) |
| `done` | run written | `{"world_id", "world_dir", "artifacts", "events", "cost_usd_segment", "cost_usd_total"}` |

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
`RunPaused`, emits `paused`, writes `job.status = "paused"` into the `.world` and exits `0`.
`SIGTERM`/`SIGINT` set the same flag (graceful), so closing the app pauses
rather than corrupts.

### 3.3 The world directory and its ledger (`worlds/file.py`, `jobs/state.py`)

A build's job **is** its world: there is no separate job directory. `world create`
mints `<worlds-root>/<world-id>/.world` (`world-id = <company-slug>-<digest12>` of
the config; minted once and kept across extends) and every later command works on
that folder.

| path | who writes | contents |
|------|-----------|----------|
| `.world` | `worlds/file.py` (atomic `tmp` + `replace` under a process-wide lock; both the pipeline and the progress sink read-modify-write it) | `world_id`, `company`, `config` (the effective `WorldConfig`, minus its destination), `config_digest`, `render_backend`, `live`, `counts`, `validation`, `outputs`, `history: [{"kind": "create"\|"extend"\|"data", "at", "changes"}]`, `lineage` (parent world when forked), **`job`** (`{"status": "created"\|"running"\|"paused"\|"failed"\|"done"\|"cancelled", "pid", "phase", "artifacts_done", "artifacts_total", "artifacts_cached", "estimate", "cost_usd_total", "cost_usd_segment", "usage_total", "segments": [{"started_at", "ended_at", "artifacts_rendered", "cost_usd", "reason"}], "started_at", "updated_at", "finished_at", "error"}`), `data` (per-scenario summaries), `evals` |
| `.cache/llm/` | `ResponseCache` | the snapshot itself — inside the world, so it moves with it |
| `.jobs/progress.jsonl` | worker (append) | every `ProgressEvent`, so the UI can rebuild the timeline after a restart |
| `.jobs/control.json` | UI / worker | `{"pause": true}` request flag; removed on resume |

The `.world` is the single source of truth every list is built from
(`enterprise_sim.worlds.summary.summarize_world` in Python, `sidecar/worlds/discover.ts`
in the explorer — the same shape); a job whose `status` is `running` but whose `pid`
is dead is shown as **interrupted** (resumable). A bare `execute_run` (no job layer
above it) writes the same `.world` with `job.status = "done"`, so a world built from
the CLI or a test is indistinguishable from one built here.

### 3.4 The worker (`jobs/worker.py`) and CLI (`worlds/cli.py`)

```
enterprise-sim world catalog                      # JSON: archetypes, playbooks (incl. templates), producers,
                                                  #       backends, models + pricing, company sizes, config defaults
enterprise-sim world estimate CFG [--live]        # JSON RenderEstimate + world/schedule counts (dry run, keyless)
enterprise-sim world create  CFG [--worlds-root DIR] [--live] [--dry-run] [--no-run]
                                                  # mints <root>/<world-id>/.world and builds it; --no-run only mints
                                                  #   (prints {"world_id","world_dir"} for a supervisor); idempotent
enterprise-sim world extend  WORLD_DIR [--period-end D] [--add-project JSON]… [--live] [--no-run]
                                                  # grows the SAME world (history entry + job reset), then builds
enterprise-sim world fork    WORLD_DIR [--seed N] [--period-end D] [--add-project JSON]… [--no-run]
                                                  # a parallel world: new folder, lineage back to the parent, cache copied
enterprise-sim world run     WORLD_DIR            # builds or RESUMES the world; streams progress JSONL on stdout
enterprise-sim world pause   WORLD_DIR            # writes .jobs/control.json; the running worker stops cooperatively
enterprise-sim world cancel  WORLD_DIR            # marks a paused/interrupted/failed build cancelled (the world is kept)
enterprise-sim world status  WORLD_DIR            # the world's summary JSON (.world + liveness)
enterprise-sim world list    [--worlds-root DIR]  # {"worlds": [summary, …]} for every .world under the root
enterprise-sim world configure WORLD_DIR --patch JSON   # deep-merge a patch into the config (e.g. raise the ceiling)
enterprise-sim world data    CFG [--world WORLD_DIR] [--worlds-root DIR]   # structured data into <world>/data/<scenario>/
enterprise-sim world adopt   OLD_RUN_DIR [--data OLD_DATA_DIR]… [--worlds-root DIR]  # migrate a pre-.world run
```

`world run` is idempotent: it reads the `.world`, opens a new **segment** in its
ledger, builds an `LLMClient` from the recorded config (`live` from the `.world`)
against the world-local cache, runs `execute_run(config, world_dir=…, progress=…,
control=…)` *into the same directory*, then **finalizes**: generates the world's
eval question set (`enterprise-sim evals generate`, cheap and deterministic — see
`EXPLORER_EVALS.md`) and marks the ledger `done`. On `RunPaused` → `paused`; on
`CostCeilingExceeded`/any exception → `failed` with the message (still resumable:
raising the ceiling in the UI is `world configure` + `world run`).

`cost_usd_total = sum(segment.cost_usd)` across the world's whole life (a later
extend appends more segments); `cost_usd_segment` is `client.cost.total_cost_usd`
of the live segment. Both are in every `artifact` event so the UI never has to add
across files.

### 3.5 Extending a world in place, or forking one (`jobs/extend.py`)

`extend_config(parent: WorldConfig, *, period_end: date | None, add_projects: Sequence[ProjectConfig]) -> WorldConfig`
derives the next config from the world's current one (`.world` → `config`):

- **more time**: `simulation.period_end` may only move later (validated);
- **more instances**: appended `[[projects]]` (each is one more scenario bound
  to a playbook — the existing ones are kept verbatim, in order);
- everything else (seed, company, model) is inherited so Layer A reproduces the
  same org.

`world extend` then **adopts** that config into the *same* `.world`
(`WorldFile.record_extend`: a history entry with the changes, the new digest, the
job reset to `created`) and re-renders into the *same* folder. The `world_id`
never changes; there is no child directory. A deliberately parallel world is
`world fork`: a new folder with `lineage = {parent_world_id, parent_world_dir,
changes}` and a copy of the parent's cache.

**Reuse guarantee (measured, not assumed).** Anchored projects draw from their
own seeded sub-stream (`SeedContext.rng("project", project_id)`) and the
scheduler seeds per `(scenario, activation)`, so:

- **More time only** (later `period_end`, nothing added): the extended world is a
  superset of the earlier one (node *and* edge ids), the earlier events' prompts are
  identical, and every earlier artifact comes back as a cache hit — the number of
  *non-cached* renders equals `new total − old total`. Locked by
  `tests/test_jobs_extend.py`.
- **Added scenario instances** (new `[[projects]]`): node ids and structural edges
  (`authored`, `reviewed`, `expresses`, `under`, …) still superset cleanly, but the
  grounding roster every prompt carries (D30 layer 1) lists *all* projects in the
  company, so every prompt changes by one line and the earlier artifacts are
  **re-rendered** (and their LLM-chosen `references` edges may differ). The UI
  therefore shows the estimate for an extension honestly: "more time" is priced at
  the new deliverables only; "more instances" is priced as a full re-render, with
  the cached/new split visible while it runs.

Because the cache is world-local there is nothing to copy for an extend; a fork
seeds its own `.cache/llm` from the parent's (`copy_cache`), so whatever prompts
*are* identical are free there too.

## 4. Sidecar (`src/sidecar/worlds/`)

`WorldManager` (pure supervision, no simulation logic) plus `discover.ts` (the
`.world` reader every list uses):

| op | params | result |
|----|--------|--------|
| `listWorlds` (core op, `index.ts`) | – | every `.world` under the worlds root as a summary (`discover.ts`; no subprocess), incl. `status` (`interrupted` when the pid is dead), `alive`, `has_kg`, `data`, `evals`, `history` |
| `worldCatalog` | – | `world catalog` JSON (cached for the process lifetime; refreshed by `templatesChanged`) |
| `worldEstimate` | `{config, live?}` | `world estimate` JSON |
| `worldCreate` | `{config, live?}` | `world create --no-run --worlds-root <root>`; `{worldId, worldDir}` |
| `worldExtend` | `{worldDir, periodEnd?, addProjects?, live?}` | `world extend --no-run`; `{worldId, worldDir, changes}` — the SAME dir |
| `worldStart` | `{worldDir}` | spawns `world run`; `{pid}`; also used for **resume** |
| `worldPause` | `{worldDir}` | `world pause`; the manager also sends `SIGTERM` if no progress within 60 s (belt and braces) |
| `worldCancel` | `{worldDir}` | pause, wait for the worker to stop, then `world cancel` (the world is kept) |
| `worldDelete` | `{worldDir}` | removes the whole world directory (refused while it is being built) |
| `worldStatus` | `{worldDir}` | the world's summary |
| `watchWorld` | `{worldDir}` | stream: replays `.jobs/progress.jsonl` (so a reattached client rebuilds the view), then live events until `unwatchWorld` |
| `unwatchWorld` | `{id}` | – |
| `readWorldConfig` | `{worldDir}` | the `.world`'s `config` + `lineage` + `history` — feeds the Extend form |
| `worldConfigure` | `{worldDir, patch}` | `world configure --patch` (deep merge, validated by Python) |

The manager tails the worker's stdout into per-job subscriber sets; when the
process exits it reads the `.world` ledger and emits a final `state` event.
`artifact` events carry both `cost_usd_segment` (this process) and
`cost_usd_total` (all segments; the worker adds the closed segments' spend).
**Estimates are computed in the renderer** from the event stream (`src/renderer/worlds/projection.ts`, unit-tested):

- `remainingSeconds = (total - done) × meanSecondsPerNonCachedArtifact` (EWMA over the current segment; cached artifacts count as 0 s);
- `projectedTotalCost = costSoFar + (total - done) × meanCostPerNonCachedArtifact`, falling back to the D13 per-artifact estimate until three real artifacts have been observed;
- render throughput accounts for concurrency implicitly (wall-clock per artifact as observed).

## 5. Renderer: the Worlds view

- **World list** (left): **every** world under the worlds root — built here or from the CLI — with a status pill (created / running / paused / interrupted / failed / done / cancelled), the company + window, a progress bar, cost, and badges for what it holds (graph `N`n/`M`e or *no graph yet*, `data ×k`, `evals N`, *extended*, *fork*). Actions: *Build* / *Resume*, *Pause*, *Cancel*, *Extend*, *Open in Explore* (only with a graph), *Delete* (the whole folder). The same list backs the topbar picker.
- **New world** (form, not a wizard — one scrollable page with sections mirroring the TOML, each with a one-line explainer): Company · Window & seed · Departments (multi-select from catalog, ordered; *a department is an org-unit archetype*) · Scenario instances (project rows: name, description, playbook, department; *a playbook is the script a project follows*) · Structured data (data scenarios to build with the world, template/llm authoring, scale, loop passes) · Model (backend, model incl. Opus 5.5, realism, `live` switch with a clear "costs money" note) · Scale (concurrency, cost ceiling, cache dir — default inside the world, per-artifact token estimates) · Worlds root. *Estimate* runs `worldEstimate`; *Create & build* runs `worldCreate` + `worldStart`. *Load from TOML…* pre-fills from any config pasted as JSON.
- **Extend world**: opened from a finished world (list or Explore topbar). Pre-filled from `readWorldConfig`; only `period_end`, new project rows, and the model/scale knobs are editable. *Extend & build* runs `worldExtend` + `worldConfigure` (model/scale) + `worldStart` — into the same folder.
- **World detail**: phase timeline, `done/total` with cached/new split, elapsed, ETA, cost so far / projected total, model + backend, *what this world holds* (graph counts, each data scenario, eval questions), the history (create / extend / data entries with their changes, fork lineage), a live log tail (last 200 progress lines), and an error banner with *Raise ceiling & resume* when the failure is `CostCeilingExceeded`.
- The empty state explains what a world is; the Templates view explains departments, playbooks and templates the same way.

## 6. Tests

Python (`tests/test_worlds_*.py`, `tests/test_jobs_*.py`): the `.world` round-trip, discovery and
adoption; the `world …` CLI; progress events on the golden config (counts match the `.world`); pause honoured mid-render on the fake backend (a control
file written from a sink callback after artifact N → `paused` state with N
artifacts, resume → `done`, byte-identical to an uninterrupted build; a paused world
moved to another directory resumes from its own cache);
ledger totals across two segments; extend guarantees (§3.5); config additions +
digest; CLI JSON round-trips.
Sidecar (`tests/worlds-*.test.ts`): projection math; `.world` discovery + summaries;
JSONL tailing + reattach replay; a spawned build end-to-end — create → pause →
resume → done → extend in place (skips when Python is unavailable).
