/** A viz side-channel event the agent emits to drive the graph UI. */
export interface VizEvent {
  type: 'highlight' | 'focus'
  nodeIds: string[]
  edgeIds?: string[]
  note?: string
}

/** Which query engine produced a turn's answer. */
export type QueryEngine = 'Cypher' | 'SPARQL'

/**
 * The engine + exact query that produced a turn's answer. Derived from the last
 * SUCCESSFUL cypher_query/sparql_query tool call of the turn, and surfaced as a
 * structured block in the UI (distinct from the live tool-trace).
 */
export interface FinalQuery {
  engine: QueryEngine
  query: string
}

export type AgentEvent =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool_use'; id: string; name: string; input: unknown }
  | { kind: 'tool_result'; id: string; ok: boolean; preview: string }
  | { kind: 'viz'; viz: VizEvent }
  | { kind: 'final_query'; engine: QueryEngine; query: string }
  | { kind: 'done'; sessionId: string | null; usage?: unknown }
  | { kind: 'error'; message: string }
