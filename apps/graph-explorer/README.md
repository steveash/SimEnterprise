# Enterprise-Sim Graph Explorer

An Electron desktop app for exploring the **gold knowledge graph** produced by an
Enterprise-Sim run: load a run, browse/search/filter the graph interactively, and ask
a Claude agent questions it answers by writing **Cypher** and **SPARQL** reasoning
queries — highlighting the answer right on the graph.

## What it does

- **Load a run** — point at any `runs/.../` directory (the one containing `kg/`). Runs
  under the repo's `runs/` are auto-discovered.
- **Interactive graph** — Cytoscape canvas with force / hierarchy / radial layouts,
  per-type colors, a legend that toggles node & edge types, and a **time scrubber** that
  dims entities created after a cursor (watch the week assemble).
- **Search → filter to neighbors** — fuzzy search resolves names/titles/props to nodes;
  select one to expand its N-hop neighborhood or trace a shortest path to another node.
- **Provenance drill-down** — every node links back to the source artifacts and the exact
  text mentions that ground it; click a mention to read the artifact.
- **Agent chat** — a Claude Agent SDK harness with in-process graph tools. It searches,
  runs Cypher (traversal) or SPARQL (reasoning over an inferred ontology), and highlights
  results. A live **tool-trace** shows every query it generated, and each turn ends with a
  structured **engine + final query** block (see below) so you can see *exactly* which
  engine answered and copy the query that did it.
- **Manual query console** — write Cypher/SPARQL yourself with schema reference + samples,
  and **save any query as a lens** (see below) to re-run it later in one click.
- **Saved lenses** — promote a console query to a named, persistent **lens**; lenses
  survive app restarts and re-run with a single click.
- **Run diff** — compare two runs to see structural drift, with a **typed breakdown**
  (per node/edge type added/removed counts) and click-through to each changed element.

## Architecture

```
Electron main  ──spawns──▶  Node sidecar (system node)
   │  (IPC: port, dialogs)        │  WebSocket RPC
   ▼                              ├── GraphIndex   (search / neighbors / paths / provenance)
Renderer (React + Cytoscape) ◀────┤── Kùzu         (embedded Cypher engine)
   ws://127.0.0.1:<port>          ├── Oxigraph     (embedded SPARQL + materialized inference)
                                  └── Claude Agent SDK (tools → engines, streams events)
```

The native query engines (Kùzu, Oxigraph) and the agent run in a **sidecar** spawned with
the *system* `node` — so the prebuilt native addons match the system ABI instead of
Electron's. The renderer talks to it over a localhost WebSocket.

### The reasoning layer (SPARQL)

The graph is a labeled property graph (no RDF natively). On load it is also projected to
RDF and an **ontology** materializes *inferred* predicates that are not in the raw data
(see `src/sidecar/graph/rdf.ts`):

- `der:manages` / `der:manages_chain` — inverse + transitive closure of `reports_to`
- `der:reports_to_chain` — transitive management chain
- `der:in_department` — person → department via team membership or leadership
- `der:advances_goal_effective` — advances a goal *or any of its parent goals*
- `der:subgoal_of_chain`, `der:subinitiative_of_chain`

This is the entailment that SPARQL buys over raw Cypher traversal. The agent is told to
prefer SPARQL when a question needs these derived facts.

### Agent engine + final-query display

Every agent turn surfaces, as a structured block, **which engine it chose** (Cypher or
SPARQL) and **the exact query** that produced the answer — distinct from the live
tool-trace, which lists *all* attempts (including failed/abandoned ones).

The block is derived purely from the turn's event stream by `finalQueryFromEvents`
(`src/sidecar/agent/harness.ts`): it takes the **last successful** `cypher_query` /
`sparql_query` tool call of the turn, labels it with its engine, and emits it as a
`final_query` event. Turns that ran no query engine surface no block. The renderer
(`ChatPanel`) renders it as a copyable, mono code block with an `engine: …` badge. This
derivation is unit-tested in `tests/agent.test.ts` and asserted on a real turn by the
gated live test.

### Saved lenses (persistence)

A **lens** is a named, reusable query promoted from the query console. Lenses persist
across app restarts as JSON in Electron's `userData` dir (`lenses.json`), managed by
`LensStore` (`src/main/lenses.ts`) and exposed to the renderer over the
`lenses-list` / `lenses-save` / `lenses-delete` IPC channels.

`LensStore` is deliberately decoupled from Electron — it takes a plain file path — so the
full save → restart → delete lifecycle (plus corrupt-file degradation and round-trip
serialization) is unit-tested under plain node in `tests/lenses.test.ts`, mocking the
`userData` path with a temp dir. The on-disk shape is validated on read by `isLens`
(`src/shared/lenses.ts`), so a corrupt or partially-malformed file degrades to dropping
only the bad entries rather than crashing.

## Run it

```bash
cd apps/graph-explorer
npm install
npm run dev        # launches Vite + Electron
```

Set `ANTHROPIC_API_KEY` in your environment to enable the agent, or paste a key into the
in-app banner. The agent model is selectable (Sonnet / Opus / Haiku).

> **Headless / container note:** if Electron aborts with a `chrome-sandbox` SUID error,
> launch with `--no-sandbox` (only needed where the sandbox helper isn't root-owned).

## Build & test

```bash
npm run typecheck   # tsc over node + web project refs (no emit)
npm run build       # electron-vite build → out/
npm run preview     # run the built bundle through Electron
```

`electron-vite build` bundles **only** the three Electron-owned entry points into `out/`:

```
out/
  main/index.js      Electron main (window + sidecar lifecycle + lens IPC)
  preload/index.js   contextBridge API
  renderer/…         the React UI (loaded via loadFile in production)
```

**In dev the sidecar is run from source.** The native query engines (Kùzu, Oxigraph) and
the Agent SDK run in a child process spawned with the **system `node`** via `tsx`, so their
prebuilt native addons match the system ABI rather than Electron's — `out/main/index.js`
spawns `node_modules/.bin/tsx src/sidecar/index.ts`. For a distributable build the sidecar
is instead esbuild-bundled and shipped with its native engines and a node binary; see
**Package an installable** below.

## Test

```bash
npm test            # vitest run (one shot)
npm run test:watch  # vitest watch
```

The suite runs under plain **node** (not Electron) — the kuzu/oxigraph addons and the Agent
SDK all load under the system node, so a node test environment is all it needs. Two kinds of
tests:

- **Always-on, deterministic** — pure-logic units that hold on any checkout: the structural
  run-diff and its typed breakdown (`tests/diff.test.ts`), the agent's MCP tool wiring and
  `finalQueryFromEvents` engine/query derivation (`tests/agent.test.ts`), and the full lens
  persistence lifecycle (`tests/lenses.test.ts`).
- **Gated** — tests that need real inputs skip cleanly when the input is absent. The
  end-to-end RPC test (`tests/rpc.test.ts`, including `diffRuns` over the wire) and the
  loader/engine tests run only when the **golden run** is present under `runs/`; the live
  agent turn runs only when **`ANTHROPIC_API_KEY`** is set. Neither is required for a green
  `npm test` on a bare checkout.

## Package an installable

```bash
cd apps/graph-explorer
npm install
npm run dist          # → dist/*.AppImage and dist/*.deb (linux x64)
```

`npm run dist` runs three steps:

1. `electron-vite build` — compiles main / preload / renderer into `out/`.
2. `npm run build:sidecar` (`scripts/build-sidecar.mjs`) — esbuilds the sidecar to a
   single `out/sidecar/index.mjs` (ws + Agent SDK inlined), stages the native engines
   (`kuzu`, `oxigraph`) into `out/sidecar/node_modules`, and copies the running `node`
   binary into `out/sidecar/node` for a hermetic app.
3. `electron-builder` — produces the AppImage + deb, shipping `out/sidecar` as an
   unpacked resource (`<resources>/sidecar`) via `extraResources`.

Run the result without a dev checkout:

```bash
./dist/*.AppImage --no-sandbox          # AppImage
# or install the deb:
sudo dpkg -i ./dist/*.deb && enterprise-sim-graph-explorer --no-sandbox
```

The packaged app loads a run (graph renders, Cypher + SPARQL queries work) with **no
dev `node_modules` on PATH** — the sidecar runs from the bundled `node` against the
staged native engines.

### Why the sidecar carries its own node

The native Cypher engine (`kuzu`) is a prebuilt `.node` addon compiled for the **system
node** ABI. Electron ships a *different* node ABI (`process.versions.modules` differs), so
it cannot load that addon — the sidecar must run under a real node binary, not the Electron
runtime. The build copies the build machine's `node` into the bundle. Set `BUNDLE_NODE=0`
to skip that copy and instead locate a system `node` at runtime (the app then requires
Node ≥ 22 on `PATH`). `oxigraph` is a WASM module, so it is ABI-independent and just rides
along as a normal package.

Runtime node resolution (main process, packaged): `GRAPH_EXPLORER_NODE` env override →
bundled `<resources>/sidecar/node` → system `node` on `PATH`.

### Platform caveats

- **`--no-sandbox`** is needed where Electron's `chrome-sandbox` helper isn't root-owned
  (containers, some CI). See the headless note above.
- The build is **single-platform**: `scripts/build-sidecar.mjs` stages the *current*
  machine's `kuzu` addon and `node` binary, so build the linux artifact on linux x64.
  macOS / Windows targets and cross-builds are tracked as follow-up work.

## Layout

```
src/
  main/        Electron main — window, sidecar lifecycle, IPC, dialogs
  preload/     contextBridge API
  sidecar/     Node host (system ABI): graph engines + agent
    graph/     loader, in-mem index, kuzu (Cypher), rdf (SPARQL + ontology)
    agent/     Claude Agent SDK harness + in-process MCP graph tools
  renderer/    React UI (Cytoscape graph, search, chat, query console, diff, …)
  shared/      model + wire-protocol + agent-event types (used by both sides)
```
