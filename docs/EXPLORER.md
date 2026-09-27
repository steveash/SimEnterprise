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

The explorer used to be a *consumer* of finished builds: point it at a directory
and browse the gold KG. It is now the place you **do** SimEnterprise. Its unit of
work is the **world** — one simulated company, one folder on disk (`enterprise_sim.worlds`)
holding the document corpus, the gold KG, and any structured data products, described
by a single `.world` file:

| View | What you do there | Doc |
|------|-------------------|-----|
| **Explore** | the original graph explorer: search, provenance, Cypher/SPARQL, agent chat, diff | `apps/graph-explorer/README.md` |
| **Worlds** | every world on disk in one list (built here or from the CLI); create a world from scratch or extend one in place; watch progress, ETA and cost; pause / resume / cancel | `EXPLORER_RUNS.md` |
| **Templates** | author new department archetypes and scenario playbooks with the `author-playbook` skill, validate them, and use them in worlds | `EXPLORER_TEMPLATES.md` |
| **Evals** | browse a world's eval question set, run one / a % / all with a live accuracy score, and propose new questions with an AI chat | `EXPLORER_EVALS.md` |

Everything the CLI can do for these three areas is reachable from the UI; the
UI never re-implements simulation logic — it drives the Python package.

## 2. Navigation

`App.tsx` gains a top-level view switcher in the topbar: **Explore · Worlds ·
Templates · Evals**. The existing four-pane layout is the *Explore* view,
unchanged. The other three are full-width views that own the center + right
area but keep the topbar (the world picker stays, because *Evals* and *Extend*
operate on the currently selected world). The picker and the Worlds tab render
**one** discovered list (`listWorlds`), so they can never disagree; a world
without an exported graph yet is listed but not selectable for Explore.

State lives in the single zustand store (`store.ts`), split into feature slices
(`store-worlds.ts`, `store-templates.ts`, `store-evals.ts`) merged into `useStore`
so cross-feature actions (e.g. "build finished → load it in Explore") are one
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
  `enterprise-sim world …`, `enterprise-sim templates …`, `enterprise-sim evals …`.
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

Everything lives in **world directories** under the **worlds root**
(`GRAPH_EXPLORER_WORLDS_ROOT`, default `<repo>/worlds`). A world is any directory
holding a `.world` file (`enterprise_sim.worlds`); the sidecar discovers worlds by
that file alone — never by `kg/` or any other side effect of a build — and both the
topbar picker and the Worlds tab render the same discovered list.

```
worlds/
  <world-id>/                        # ONE world: one simulated company, everything it holds
    .world                           # the single state file: identity, effective config, counts,
                                     #   validation summary, history (create / extend / data), lineage,
                                     #   the job ledger (status / progress / cost / segments), data + evals summaries
    .cache/llm/                      # the response cache (D31) — inside the world, so it moves with it
    .jobs/control.json               # cooperative-pause request (transient)
    .jobs/progress.jsonl             # append-only progress log the UI replays (a log, not state)
    organization/  artifacts/  kg/  validation/     # the corpus + gold KG
    evals/questions.jsonl            # this world's eval question set (docs/EXPLORER_EVALS.md)
    evals/results/<eval-id>/…        # one directory per eval execution
    data/<scenario>/                 # structured data products (docs/DATA_PRODUCTS.md)
templates/                           # repo root, not worlds/: user-authored plugins
  <slug>/template.json  plugin.py  test_<slug>.py
```

There are no `manifest.json` / `config.snapshot.json` / `lineage.json` /
`state.json` files any more and no separate jobs root: a world folder can be
zipped, moved and unzipped and everything — including pause/resume, which lives
on the world-local cache — keeps working. `enterprise-sim world adopt` migrates
a pre-`.world` run directory (and its old standalone data-run folders) into
this layout.

Environment variables (all optional):

| Var | Meaning |
|-----|---------|
| `GRAPH_EXPLORER_WORLDS_ROOT` | worlds root (the old `GRAPH_EXPLORER_WORLDS_ROOT` name is still honored) |
| `GRAPH_EXPLORER_PYTHON_CMD` | command prefix for the Python CLI (§3) |
| `GRAPH_EXPLORER_TEMPLATES_DIR` | templates dir (default `<repo>/templates`) |
| `ENTERPRISE_SIM_PLUGIN_PATH` | `os.pathsep`-separated extra plugin dirs the **Python** side discovers (the sidecar sets it to the templates dir for every spawned build) |

## 5. Testing rules

- **Python**: everything new is keyless and deterministic on the `fake` backend
  and runs in `./scripts/gate.sh` (ruff + mypy strict + pytest). Progress,
  pause/resume, extension, template validation and eval scoring are all unit
  tested without a network. Anything that calls a real model is gated exactly
  like the benchmark runners (`tests/test_benchmark_keyless.py::requires_llm_runner`).
- **Sidecar / renderer** (`npm test`, vitest under plain node): pure logic
  (JSONL parsing, `.world` discovery + summaries, ETA/cost projection, eval scoring port,
  sampling) is always-on; tests that need the Python CLI spawn it and skip if
  `uv`/`enterprise-sim` is unavailable; agent turns skip without
  `ANTHROPIC_API_KEY`. `npm run typecheck` must stay green.
- The golden world stays the shared fixture: `enterprise-sim world create examples/golden.toml`
  produces `worlds/golden-slice-co-…` — the world the sidecar tests, the eval browser, and the extend flow all use.

## 6. Delivery plan

Implemented in two waves so the Python contracts exist before the UI consumes them:

1. **Python** — `enterprise_sim/jobs/` (progress, control, state, worker, extend),
   config additions (`departments`, project `playbook`/`department`, `plugins`),
   external plugin discovery, `enterprise_sim/templates/`, `enterprise_sim/evals/`,
   and the three CLI groups.
2. **Explorer** — sidecar `python.ts` bridge + `jobs/`, `templates/`, `evals/`
   modules; renderer views and store slices; docs in `apps/graph-explorer/README.md`.
