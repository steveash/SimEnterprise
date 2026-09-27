// Wire types for the Worlds view (docs/EXPLORER_RUNS.md §4). Types that mirror a
// Python-produced JSON document (ProgressEvent, JobState, Segment, and the world
// summary `enterprise_sim.worlds.summary` builds from a `.world`) keep the exact
// snake_case field names Python writes, so a payload can be typed without any
// renaming layer between Python and the renderer. Op param/result types the
// sidecar itself defines are camelCase, matching the op table in the doc.

/** One line of `<world>/.jobs/progress.jsonl` / a `world run` stdout line (jobs/progress.py). */
export interface ProgressEvent {
  kind: string // 'phase' | 'world_built' | 'scheduled' | 'estimate' | 'artifact' | 'paused' | 'error' | 'done' | 'warning'
  ts: number
  data: Record<string, unknown>
}

/** One render attempt within a world's job ledger (jobs/state.py `Segment`). */
export interface Segment {
  started_at: string
  ended_at: string | null
  artifacts_rendered: number
  cost_usd: number
  reason: string | null // 'paused' | 'done' | 'failed' | 'killed'
}

export type JobStatus = 'created' | 'running' | 'paused' | 'failed' | 'done' | 'cancelled'

/** The `job` block of a `.world` (jobs/state.py `JobState`): the build ledger. */
export interface JobState {
  status: JobStatus
  pid: number | null
  phase: string | null
  artifacts_done: number
  artifacts_total: number
  artifacts_cached: number
  estimate: Record<string, unknown> | null
  cost_usd_total: number
  cost_usd_segment: number
  usage_total: Record<string, number>
  segments: Segment[]
  started_at: string | null
  updated_at: string | null
  finished_at: string | null
  error: { message: string; type: string } | null
}

/** `JobStatus` plus the derived `interrupted` (status `running` but the pid is dead). */
export type WorldStatus = JobStatus | 'interrupted'

/** One structured-data scenario under `<world>/data/`, as summarized in the `.world`. */
export interface DataScenarioSummary {
  title: string | null
  rows: number
  tables: number
  questions: { total?: number; answerable?: number }
  iterations: number
  llm_cost_usd: number
  created_at: string | null
}

export interface WorldHistoryEntry {
  kind: string // 'create' | 'extend' | 'data' | …
  at: string | null
  changes?: Record<string, unknown> | null
}

export interface WorldLineage {
  parent_world_id?: string | null
  parent_world_dir?: string | null
  changes?: Record<string, unknown>
}

/**
 * One world, summarized for a list — the shape `enterprise-sim world list` /
 * `world status` print and `sidecar/worlds/discover.ts` builds straight from a
 * `.world` file. Every world list in the app (the topbar picker, the Worlds
 * tab) renders from this, so they can never disagree about what a world is.
 */
export interface WorldSummary {
  world_id: string
  world_dir: string
  schema_version: string
  company: { name: string; vertical?: string; size?: string; description?: string | null }
  seed: number
  window: { start?: string; end?: string }
  counts: { nodes?: number; edges?: number; events?: number }
  render_backend: string
  live: boolean
  config_digest: string
  created_at: string
  updated_at: string
  status: WorldStatus
  /** Whether `job.pid` is a live process on this machine (`process.kill(pid, 0)`). */
  alive: boolean
  job: JobState
  /** `kg/nodes.jsonl` exists — the world can be opened in Explore. */
  has_kg: boolean
  data: Record<string, DataScenarioSummary>
  evals: { questions?: number }
  history: WorldHistoryEntry[]
  lineage: WorldLineage | null
}

// ---- sidecar op params/results (docs/EXPLORER_RUNS.md §4) ----

export interface WorldEstimateParams {
  config: Record<string, unknown>
  live?: boolean
}

export interface WorldCreateParams {
  config: Record<string, unknown>
  live?: boolean
}

export interface WorldCreateResult {
  worldId: string
  worldDir: string
}

export interface WorldExtendParams {
  worldDir: string
  periodEnd?: string // YYYY-MM-DD
  addProjects?: Record<string, unknown>[] // ProjectConfig JSON, appended
  live?: boolean
}

export interface WorldExtendResult {
  worldId: string
  worldDir: string
  changes: Record<string, unknown>
}

export interface WorldStartResult {
  pid: number | undefined
}

export interface WorldListResult {
  worlds: WorldSummary[]
}

/** A `watchWorld` stream event: a replayed/live progress line, or the terminal ledger. */
export type WatchWorldEvent = { kind: 'progress'; event: ProgressEvent } | { kind: 'state'; state: JobState | null }

/** What the Extend form pre-fills from: the world's current config + how it got here. */
export interface ReadWorldConfigResult {
  config: Record<string, unknown> | null
  lineage: WorldLineage | null
  history: WorldHistoryEntry[]
}

export interface WorldConfigureParams {
  worldDir: string
  /** Deep-merged onto the world's config (nested objects merge key-by-key; other values replace). */
  patch: Record<string, unknown>
}
