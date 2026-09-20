// Pure ETA/cost projection over a job's progress-event stream
// (docs/EXPLORER_RUNS.md §4). The sidecar only reports what happened; this
// module turns that into what a UI shows: elapsed/remaining time, cost so far,
// and a projected total. No I/O, no timers — every function takes whatever
// "now" it needs as a parameter, so it is trivially unit-testable with
// synthetic event sequences.
import type { JobState, ProgressEvent } from '../../shared/jobs-protocol.js'

/** Smoothing factor for the running-mean-seconds/cost-per-artifact EWMA. */
const EWMA_ALPHA = 0.35
/** How many real (non-cached) artifacts must land before the EWMA replaces the D13 estimate. */
const MIN_SAMPLES_BEFORE_EWMA = 3

export type JobPhase =
  | 'idle'
  | 'world'
  | 'schedule'
  | 'estimate'
  | 'render'
  | 'assemble'
  | 'evals'
  | 'done'
  | 'paused'
  | 'failed'
  | 'cancelled'

export interface ProjectionState {
  phase: JobPhase
  status: 'idle' | 'running' | 'paused' | 'failed' | 'done' | 'cancelled'
  done: number
  total: number
  cached: number
  /** Cost across every segment (this run + any prior pause/resume), as the worker reports it. */
  costSoFar: number
  projectedTotalCost: number | null
  /** ms epoch of the first event this projection saw (render-clock start). */
  startedAtMs: number | null
  /** ms epoch of the most recently applied event. */
  lastEventAtMs: number | null
  remainingSeconds: number | null
  meanSecondsPerArtifact: number | null
  /** Running count of observed non-cached ("real") artifacts, for the EWMA warm-up. */
  realArtifactCount: number
  /** From the `estimate` event (the D13 gate): the per-artifact cost estimate. */
  estimatedCostPerArtifact: number | null
  error: { message: string; type: string } | null
  runId: string | null
  runDir: string | null
  model: string | null
  /** Last N progress events, most recent last — a ready-made log tail. */
  log: ProgressEvent[]
}

const LOG_LIMIT = 200

export function initProjection(): ProjectionState {
  return {
    phase: 'idle',
    status: 'idle',
    done: 0,
    total: 0,
    cached: 0,
    costSoFar: 0,
    projectedTotalCost: null,
    startedAtMs: null,
    lastEventAtMs: null,
    remainingSeconds: null,
    meanSecondsPerArtifact: null,
    realArtifactCount: 0,
    estimatedCostPerArtifact: null,
    error: null,
    runId: null,
    runDir: null,
    model: null,
    log: []
  }
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

function pushLog(log: ProgressEvent[], event: ProgressEvent): ProgressEvent[] {
  const next = [...log, event]
  return next.length > LOG_LIMIT ? next.slice(next.length - LOG_LIMIT) : next
}

/** Apply one `ProgressEvent` (a live line or a replayed one) to `state`. Pure. */
export function reduceEvent(state: ProjectionState, event: ProgressEvent): ProjectionState {
  const tsMs = event.ts * 1000
  const startedAtMs = state.startedAtMs ?? tsMs
  const data = event.data
  let next: ProjectionState = { ...state, startedAtMs, lastEventAtMs: tsMs, log: pushLog(state.log, event) }

  switch (event.kind) {
    case 'phase': {
      const phase = data.phase
      if (typeof phase === 'string') next = { ...next, phase: phase as JobPhase, status: 'running' }
      break
    }
    case 'scheduled': {
      const total = num(data.artifacts_total)
      if (total !== undefined) next = { ...next, total }
      break
    }
    case 'estimate': {
      const totalCost = num(data.estimated_cost_usd)
      const totalArtifacts = num(data.artifacts_total)
      if (totalCost !== undefined && totalArtifacts) {
        next = { ...next, estimatedCostPerArtifact: totalCost / totalArtifacts }
      }
      const model = data.model
      if (typeof model === 'string') next = { ...next, model }
      break
    }
    case 'artifact': {
      const done = num(data.done)
      const total = num(data.total)
      const cached = data.cached === true
      const cost = num(data.cost_usd_total) ?? num(data.cost_usd_segment)
      if (done !== undefined) next = { ...next, done }
      if (total !== undefined) next = { ...next, total }
      if (cached) next = { ...next, cached: next.cached + 1 }
      if (cost !== undefined) next = { ...next, costSoFar: cost }

      // Wall-clock-per-artifact sample: the delta since the last artifact event.
      // A cached artifact renders effectively free — it does not feed the
      // non-cached EWMA, but it still advances `lastEventAtMs` so the *next*
      // real artifact's delta isn't inflated by the cached one's near-zero time.
      const prevMs = state.lastEventAtMs ?? startedAtMs
      const deltaSeconds = Math.max(0, (tsMs - prevMs) / 1000)
      if (!cached) {
        const count = state.realArtifactCount + 1
        const mean =
          state.meanSecondsPerArtifact === null
            ? deltaSeconds
            : EWMA_ALPHA * deltaSeconds + (1 - EWMA_ALPHA) * state.meanSecondsPerArtifact
        next = { ...next, realArtifactCount: count, meanSecondsPerArtifact: mean }
      }
      next = { ...next, ...projectRemaining(next) }
      break
    }
    case 'paused': {
      next = { ...next, status: 'paused', phase: 'paused' }
      break
    }
    case 'error': {
      const message = typeof data.message === 'string' ? data.message : 'job failed'
      const type = typeof data.type === 'string' ? data.type : 'Error'
      next = { ...next, status: 'failed', phase: 'failed', error: { message, type } }
      break
    }
    case 'done': {
      const runId = data.run_id
      const runDir = data.run_dir
      const cost = num(data.cost_usd_total)
      next = {
        ...next,
        status: 'done',
        phase: 'done',
        runId: typeof runId === 'string' ? runId : next.runId,
        runDir: typeof runDir === 'string' ? runDir : next.runDir,
        costSoFar: cost ?? next.costSoFar,
        remainingSeconds: 0,
        projectedTotalCost: cost ?? next.costSoFar
      }
      break
    }
    default:
      break
  }
  return next
}

/** Compute `remainingSeconds` + `projectedTotalCost` from the current counters. Pure. */
function projectRemaining(state: ProjectionState): Pick<ProjectionState, 'remainingSeconds' | 'projectedTotalCost'> {
  const remainingArtifacts = Math.max(0, state.total - state.done)
  const remainingSeconds =
    state.meanSecondsPerArtifact !== null ? remainingArtifacts * state.meanSecondsPerArtifact : null

  // Once 3+ real (non-cached) artifacts have landed, use the observed mean
  // cost per non-cached artifact this segment; until then, fall back to the
  // D13 gate's per-artifact estimate (may itself be null before it arrives).
  const nonCachedDone = Math.max(0, state.done - state.cached)
  const meanCostPerArtifact =
    state.realArtifactCount >= MIN_SAMPLES_BEFORE_EWMA && nonCachedDone > 0
      ? state.costSoFar / nonCachedDone
      : state.estimatedCostPerArtifact

  const projectedTotalCost =
    meanCostPerArtifact !== null ? state.costSoFar + remainingArtifacts * meanCostPerArtifact : null

  return { remainingSeconds, projectedTotalCost }
}

/** Merge the authoritative terminal `state.json` (read on process exit) onto a live projection. */
export function reduceState(state: ProjectionState, jobState: JobState | null): ProjectionState {
  if (!jobState) return state
  const statusMap: Record<string, ProjectionState['status']> = {
    running: 'running',
    paused: 'paused',
    failed: 'failed',
    done: 'done',
    cancelled: 'cancelled',
    created: 'idle'
  }
  return {
    ...state,
    status: statusMap[jobState.status] ?? state.status,
    phase: (jobState.phase as JobPhase | null) ?? state.phase,
    done: jobState.artifacts_done,
    total: jobState.artifacts_total || state.total,
    cached: jobState.artifacts_cached,
    costSoFar: jobState.cost_usd_total,
    error: jobState.error,
    runId: jobState.run_id ?? state.runId,
    runDir: jobState.run_dir ?? state.runDir,
    remainingSeconds: jobState.status === 'done' || jobState.status === 'failed' ? 0 : state.remainingSeconds
  }
}

/** Elapsed wall-clock seconds since the first event this projection applied, as of `nowMs`. */
export function elapsedSeconds(state: ProjectionState, nowMs: number = Date.now()): number {
  if (state.startedAtMs === null) return 0
  const endMs = state.status === 'done' || state.status === 'failed' || state.status === 'cancelled'
    ? (state.lastEventAtMs ?? nowMs)
    : nowMs
  return Math.max(0, (endMs - state.startedAtMs) / 1000)
}

/** Fold a whole event batch through `reduceEvent`, in order. Convenience for replay. */
export function reduceEvents(state: ProjectionState, events: readonly ProgressEvent[]): ProjectionState {
  return events.reduce(reduceEvent, state)
}
