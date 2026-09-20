# Graph Explorer as the one-stop SimEnterprise interface

> Design + user guide for the explorer's operational features: **launching and
> extending runs**, **authoring templates**, and **running evals** — all from the
> desktop/browser UI under [`apps/graph-explorer/`](../apps/graph-explorer/).
> Each feature has its own doc:
> [`EXPLORER_RUNS.md`](./EXPLORER_RUNS.md) ·
> [`EXPLORER_TEMPLATES.md`](./EXPLORER_TEMPLATES.md) ·
> [`EXPLORER_EVALS.md`](./EXPLORER_EVALS.md).
> This page holds what they share: the navigation, the Python ⇄ sidecar bridge,
> on-disk conventions, and the testing rules.

---

## 1. Goal

The explorer used to be a *consumer* of finished runs: point it at a `runs/…`
directory and browse the gold KG. It is now the place you **do** SimEnterprise:

| View | What you do there | Doc |
|------|-------------------|-----|
| **Explore** | the original graph explorer: search, provenance, Cypher/SPARQL, agent chat, diff | `apps/graph-explorer/README.md` |
| **Runs** | create a run from scratch or extend an existing one; watch progress, ETA and cost; pause / resume / cancel | `EXPLORER_RUNS.md` |
| **Templates** | author new department archetypes and scenario playbooks with the `author-playbook` skill, validate them, and use them in runs | `EXPLORER_TEMPLATES.md` |
| **Evals** | browse a run's eval question set, run one / a % / all with a live accuracy score, and propose new questions with an AI chat | `EXPLORER_EVALS.md` |

Everything the CLI can do for these three areas is reachable from the UI; the
UI never re-implements simulation logic — it drives the Python package.

## 2. Navigation

`App.tsx` gains a top-level view switcher in the topbar: **Explore · Runs ·
Templates · Evals**. The existing four-pane layout is the *Explore* view,
unchanged. The other three are full-width views that own the center + right
area but keep the topbar (run selector stays, because *Evals* and *Extend run*
operate on the currently selected run).

State lives in the single zustand store (`store.ts`), split into feature slices
(`store-runs.ts`, `store-templates.ts`, `store-evals.ts`) merged into `useStore`
so cross-feature actions (e.g. "run finished → load it in Explore") are one
`set()` away.

## 3. The Python ⇄ sidecar bridge

The simulator is Python; the explorer's backend is the Node **sidecar**
(`src/sidecar/index.ts`, WebSocket RPC). The bridge is deliberately dumb:

```
renderer ──ws rpc──▶ sidecar ──spawn──▶ enterprise-sim <subcommand> --json
                        ▲                     │ stdout: one JSON object per line
                        └──── stream events ──┘ (progress / results), exit code
```

- **Every UI-facing Python entry point is a CLI subcommand that speaks JSON**:
  `enterprise-sim job …`, `enterprise-sim templates …`, `enterprise-sim evals …`.
  One-shot commands print a single JSON document; long-running commands stream
  **JSON Lines** progress on stdout and persist their state to disk so the UI can
  re-attach after a restart. Human-readable output goes to stderr.
- The sidecar resolves the interpreter once (`src/sidecar/python.ts`):
  1. `GRAPH_EXPLORER_PYTHON_CMD` if set (a shell-split command prefix, e.g.
     `uv run --project /path/to/SimEnterprise enterprise-sim`);
  2. else, if the repo root (two levels above the app) has a `pyproject.toml`,
     `uv run --project <repo> enterprise-sim`;
  3. else `enterprise-sim` on `PATH`.
  `pythonCommand(args)` returns `{cmd, args}`; `runJson(args)` runs to completion
  and parses stdout; `streamJsonl(args, onLine)` spawns and yields parsed lines,
  returning a handle with `pid`, `kill()`, and `done: Promise<exitCode>`.
- **Long-running work never blocks the WebSocket.** Jobs run as child processes
  the sidecar supervises; the renderer subscribes with a `watch*` op whose reply
  is a stream (`{type:'stream', id, event}` — the same envelope the agent chat
  already uses) and unsubscribes by id.
- The sidecar holds no durable state. Everything a view lists comes from disk
  (see §4), so killing the app mid-run loses nothing but the live socket.

The Claude Agent SDK harness (`src/sidecar/agent/harness.ts`) is reused for the
three conversational features (Explore chat, template authoring, eval
proposals) with different tool sets and system prompts; see the feature docs.

## 4. On-disk conventions

All paths are under the **runs root** (`GRAPH_EXPLORER_RUNS_ROOT`, default
`<repo>/runs`) unless noted.

```
runs/
  <output-subdir>/<run-id>/          # a finished run, exactly as `enterprise-sim run` writes it
    manifest.json  config.snapshot.json  kg/  artifacts/  organization/  validation/
    lineage.json                     # NEW (only on extended runs): parent run + what changed
    evals/                           # NEW: this run's eval question set + results
      questions.jsonl                #   one QAPair per line (docs/EXPLORER_EVALS.md)
      results/<eval-id>/…            #   one directory per eval execution
  .jobs/<job-id>/                    # NEW: a run *job* (in-flight or finished), see EXPLORER_RUNS.md
    job.json  state.json  progress.jsonl  control.json  config.json  llm-cache/  worker.log
templates/                           # NEW (repo root, not runs/): user-authored plugins
  <slug>/template.json  plugin.py  test_<slug>.py
```

Environment variables (all optional):

| Var | Meaning |
|-----|---------|
| `GRAPH_EXPLORER_RUNS_ROOT` | runs root (existing) |
| `GRAPH_EXPLORER_PYTHON_CMD` | command prefix for the Python CLI (§3) |
| `GRAPH_EXPLORER_TEMPLATES_DIR` | templates dir (default `<repo>/templates`) |
| `ENTERPRISE_SIM_PLUGIN_PATH` | `os.pathsep`-separated extra plugin dirs the **Python** side discovers (the sidecar sets it to the templates dir for every spawned job) |

## 5. Testing rules

- **Python**: everything new is keyless and deterministic on the `fake` backend
  and runs in `./scripts/gate.sh` (ruff + mypy strict + pytest). Progress,
  pause/resume, extension, template validation and eval scoring are all unit
  tested without a network. Anything that calls a real model is gated exactly
  like the benchmark runners (`tests/test_benchmark_keyless.py::requires_llm_runner`).
- **Sidecar / renderer** (`npm test`, vitest under plain node): pure logic
  (JSONL parsing, job state reduction, ETA/cost projection, eval scoring port,
  sampling) is always-on; tests that need the Python CLI spawn it and skip if
  `uv`/`enterprise-sim` is unavailable; agent turns skip without
  `ANTHROPIC_API_KEY`. `npm run typecheck` must stay green.
- The golden run stays the shared fixture: `enterprise-sim run examples/golden.toml -o runs`
  produces a run the sidecar tests, the eval browser, and the extend flow all use.

## 6. Delivery plan

Implemented in two waves so the Python contracts exist before the UI consumes them:

1. **Python** — `enterprise_sim/jobs/` (progress, control, state, worker, extend),
   config additions (`departments`, project `playbook`/`department`, `plugins`),
   external plugin discovery, `enterprise_sim/templates/`, `enterprise_sim/evals/`,
   and the three CLI groups.
2. **Explorer** — sidecar `python.ts` bridge + `jobs/`, `templates/`, `evals/`
   modules; renderer views and store slices; docs in `apps/graph-explorer/README.md`.
