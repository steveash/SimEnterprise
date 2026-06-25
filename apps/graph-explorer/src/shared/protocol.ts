// Wire protocol between renderer and the Node sidecar (over a localhost WebSocket).
import type { GraphModel, RunSummary } from './model.js'

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

export interface LoadRunResult {
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

export interface DiffResult {
  a: RunSummary
  b: RunSummary
  nodes: { added: string[]; removed: string[]; common: string[] }
  edges: { added: string[]; removed: string[]; common: string[] }
}

export const OPS = {
  listRuns: 'listRuns',
  loadRun: 'loadRun',
  search: 'search',
  neighbors: 'neighbors',
  shortestPath: 'shortestPath',
  provenance: 'provenance',
  readArtifact: 'readArtifact',
  cypher: 'cypher',
  sparql: 'sparql',
  diffRuns: 'diffRuns',
  chat: 'chat',
  cancelChat: 'cancelChat'
} as const
