// Wire protocol between renderer and the Node sidecar (over a localhost WebSocket).
import type { GraphModel } from './model.js'

export interface RpcRequest {
  type: 'rpc'
  id: string
  op: string
  params?: unknown
}

export interface RpcResponse {
  type: 'rpc_result'
  id: string
  ok: boolean
  result?: unknown
  error?: string
}

// Streamed agent events share the request id of the originating `chat` call.
export interface RpcStreamEvent {
  type: 'stream'
  id: string
  event: unknown // AgentEvent (see sidecar/agent/harness)
}

export type ServerMessage = RpcResponse | RpcStreamEvent

export interface LoadWorldResult {
  model: GraphModel
  kuzuSchema: string
  sparqlSchema: string
  inferredCount: number
  eval: EvalSummary | null
}

export interface EvalSummary {
  metrics: { name: string; score: number; ok: boolean; detail?: string }[]
  ok: boolean
}

/** A node that appears on exactly one side of a diff, with display metadata. */
export interface DiffNode {
  id: string
  type: string
  label: string
}

/** An edge that appears on exactly one side of a diff, with endpoint labels. */
export interface DiffEdge {
  id: string
  type: string
  src: string
  dst: string
  srcLabel: string
  dstLabel: string
}

/** Per-type addition/removal counts (one row per node-type or edge-type that changed). */
export interface TypeDelta {
  type: string
  added: number
  removed: number
}

/** One side of a diff: the loaded world's identity + size (not the full `.world` summary). */
export interface DiffSide {
  worldId: string
  worldPath: string
  nodeCount: number
  edgeCount: number
  window: { start: string; end: string } | null
}

export interface DiffResult {
  a: DiffSide
  b: DiffSide
  // Raw id partitions (added = only in B, removed = only in A, common = both).
  nodes: { added: string[]; removed: string[]; common: string[] }
  edges: { added: string[]; removed: string[]; common: string[] }
  // Typed details for the changed items, resolved against whichever side has them.
  nodeChanges: { added: DiffNode[]; removed: DiffNode[] }
  edgeChanges: { added: DiffEdge[]; removed: DiffEdge[] }
  // Per-type breakdowns of additions/removals, sorted by total magnitude.
  nodeTypeDeltas: TypeDelta[]
  edgeTypeDeltas: TypeDelta[]
}

export const OPS = {
  listWorlds: 'listWorlds',
  loadWorld: 'loadWorld',
  search: 'search',
  neighbors: 'neighbors',
  shortestPath: 'shortestPath',
  provenance: 'provenance',
  readArtifact: 'readArtifact',
  cypher: 'cypher',
  sparql: 'sparql',
  diffWorlds: 'diffWorlds',
  chat: 'chat',
  cancelChat: 'cancelChat'
} as const
