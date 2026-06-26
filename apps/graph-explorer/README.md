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
  results. A live **tool-trace** shows every query it generated.
- **Manual query console** — write Cypher/SPARQL yourself with schema reference + samples.
- **Run diff** — compare two runs to see structural drift (added/removed nodes & edges).

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

## Run it

```bash
cd apps/graph-explorer
npm install
npm run dev        # launches Vite + Electron
```

Set `ANTHROPIC_API_KEY` in your environment to enable the agent, or paste a key into the
in-app banner. The agent model is selectable (Sonnet / Opus / Haiku).

Build a production bundle: `npm run build`. Typecheck: `npm run typecheck`.

> **Headless / container note:** if Electron aborts with a `chrome-sandbox` SUID error,
> launch with `--no-sandbox` (only needed where the sandbox helper isn't root-owned).

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
