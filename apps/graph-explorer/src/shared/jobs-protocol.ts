// Wire types for the Runs view (docs/EXPLORER_RUNS.md §4). Types that mirror a
// Python-produced JSON document (ProgressEvent, JobState, Segment, the job.json
// summary) keep the exact snake_case field names `enterprise_sim.jobs` writes,
// so a payload can be typed without any renaming layer between Python and the
// renderer. Op param/result types the sidecar itself defines are camelCase,
// matching the op table in the doc.

/** One line of `<job>/progress.jsonl` / a `job run` stdout line (jobs/progress.py). */
export interface ProgressEvent {
  kind: string // 'phase' | 'world_built' | 'scheduled' | 'estimate' | 'artifact' | 'paused' | 'error' | 'done' | 'warning'
  ts: number
  data: Record<string, unknown>
}

/** One render attempt within a job (jobs/state.py `Segment`). */
export interface Segment {
  started_at: string
  ended_at: string | null
  artifacts_rendered: number
  cost_usd: number
  reason: string | null // 'paused' | 'done' | 'failed' | 'killed'
}

export type JobStatus = 'created' | 'running' | 'paused' | 'failed' | 'done' | 'cancelled'

/** The contents of `<job>/state.json` (jobs/state.py `JobState`) — a UI lists jobs from this. */
export interface JobState {
  status: JobStatus
  pid: number | null
  run_id: string | null
  run_dir: string | null
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

/** The immutable request a job was created from (`<job>/job.json`, trimmed for listing). */
export interface JobSummary {
  job_id: string
  job_dir: string
  state: JobState
  job: {
    kind?: 'new' | 'extend'
    live?: boolean
    created_at?: string
    parent_run_dir?: string | null
  }
  /** Whether `state.pid` is a live process on this machine (`process.kill(pid, 0)`). */
  alive: boolean
}

// ---- sidecar op params/results (docs/EXPLORER_RUNS.md §4) ----

export interface JobEstimateParams {
  config: Record<string, unknown>
  live?: boolean
}

export interface JobExtendRequest {
  parentRunDir: string
  periodEnd?: string // YYYY-MM-DD
  addProjects?: Record<string, unknown>[] // ProjectConfig JSON, appended
}

export interface JobCreateParams {
  config: Record<string, unknown>
  live?: boolean
  extend?: JobExtendRequest
}

export interface JobCreateResult {
  jobId: string
  jobDir: string
}

export interface JobStartResult {
  pid: number | undefined
}

export interface JobListResult {
  jobs: JobSummary[]
}

/** A `watchJob` stream event: a replayed/live progress line, or the terminal state. */
export type WatchJobEvent = { kind: 'progress'; event: ProgressEvent } | { kind: 'state'; state: JobState | null }

export interface ReadRunConfigResult {
  config: Record<string, unknown> | null
  lineage: Record<string, unknown> | null
}

export interface JobUpdateConfigParams {
  jobDir: string
  /** Deep-merged onto `config.json` (nested objects merge key-by-key; other values replace). */
  patch: Record<string, unknown>
}
