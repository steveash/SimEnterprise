/** A viz side-channel event the agent emits to drive the graph UI. */
export interface VizEvent {
  type: 'highlight' | 'focus'
  nodeIds: string[]
  edgeIds?: string[]
  note?: string
}

export type AgentEvent =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool_use'; id: string; name: string; input: unknown }
  | { kind: 'tool_result'; id: string; ok: boolean; preview: string }
  | { kind: 'viz'; viz: VizEvent }
  | { kind: 'done'; sessionId: string | null; usage?: unknown }
  | { kind: 'error'; message: string }
