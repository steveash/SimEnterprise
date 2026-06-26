// Canonical in-memory shape of a loaded Enterprise-Sim gold KG run.
// Mirrors the kg/*.jsonl files produced by `enterprise-sim run`.

export interface KGNode {
  id: string
  type: string // Person | Team | Department | Company | Goal | Initiative | Project | Artifact | CalendarEvent
  label: string // best display name (title/name/aliases[0]/id)
  aliases: string[]
  created_at: string
  props: Record<string, unknown>
}

export interface KGEdge {
  id: string
  type: string // reports_to | member_of | collaborates_with | leads | owns | advances_goal | ...
  src: string
  dst: string
  created_at: string
  props: Record<string, unknown>
}

export interface KGMention {
  artifact_path: string
  entity_id: string
  surface_form: string
  locator: { medium: string; offset: number; length: number; line: number }
}

export interface KGEvent {
  id: string
  type: string
  timestamp: string
  actors: Record<string, unknown>
  subjects: string[]
  initiative?: string | null
  project?: string | null
  parent_event?: string | null
  payload?: Record<string, unknown>
  deliverable?: Record<string, unknown> | null
}

export interface KGProvenance {
  target_id: string
  artifacts: { path: string }[]
}

export interface KGAlias {
  entity_id: string
  canonical: string
  aliases: string[]
}

export interface ValidationIssue {
  kind: string
  [k: string]: unknown
}

export interface GraphModel {
  runId: string
  runPath: string
  nodes: KGNode[]
  edges: KGEdge[]
  mentions: KGMention[]
  events: KGEvent[]
  provenance: KGProvenance[]
  aliases: KGAlias[]
  validation: ValidationIssue[]
  manifest: Record<string, unknown> | null
  nodeTypes: string[]
  edgeTypes: string[]
  /** ISO range of the simulated window, derived from manifest or event timestamps. */
  timeRange: { start: string; end: string } | null
}

export interface RunSummary {
  runId: string
  runPath: string
  nodeCount: number
  edgeCount: number
  window: { start: string; end: string } | null
}
