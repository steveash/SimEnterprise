// Feature-op registry for the sidecar RPC dispatcher.
//
// The core ops (listRuns / loadRun / search / … / chat) live in `index.ts`'s
// switch. Feature modules (jobs, templates, evals) register theirs here so each
// feature owns its own file and the dispatcher never grows a giant switch. A
// handler receives an `OpContext`; it may reply once (its return value) and may
// push any number of stream events first (`ctx.stream`), which reach the
// renderer as `{type:'stream', id:<request id>, event}` — the same envelope the
// agent chat uses.
import type { LoadRunResult } from '../shared/protocol.js'
import type { GraphIndex } from './graph/index.js'
import type { KuzuEngine } from './graph/kuzu.js'
import type { OxigraphEngine } from './graph/rdf.js'

/** One fully-loaded run: model + index + both query engines (mirrors index.ts). */
export interface LoadedRun {
  index: GraphIndex
  kuzu: KuzuEngine
  oxigraph: OxigraphEngine
  result: LoadRunResult
}

export interface OpContext {
  /** The RPC request id; stream events for this request carry it. */
  requestId: string
  params: Record<string, unknown>
  /** Load (or fetch the cached) run at `runPath`. */
  ensureLoaded: (runPath: string) => Promise<LoadedRun>
  /** Push a stream event to the caller (before the final reply). */
  stream: (event: unknown) => void
  /** The runs root the sidecar discovers runs under. */
  runsRoot: string
  /** The repo root (two levels above the app) — where `pyproject.toml`, `templates/`, `skills/` live. */
  repoRoot: string
}

export type OpHandler = (ctx: OpContext) => Promise<unknown>

const HANDLERS = new Map<string, OpHandler>()

/** Register feature ops. Re-registering a name replaces it (useful in tests). */
export function registerOps(ops: Record<string, OpHandler>): void {
  for (const [name, handler] of Object.entries(ops)) HANDLERS.set(name, handler)
}

export function getOp(name: string): OpHandler | undefined {
  return HANDLERS.get(name)
}

export function registeredOps(): string[] {
  return [...HANDLERS.keys()].sort()
}
