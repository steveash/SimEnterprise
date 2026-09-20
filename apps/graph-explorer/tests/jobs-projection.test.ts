// Pure ETA/cost projection math (docs/EXPLORER_RUNS.md §4/§6). Synthetic event
// sequences only — no I/O, no timers.
import { describe, expect, it } from 'vitest'
import {
  elapsedSeconds,
  initProjection,
  reduceEvent,
  reduceEvents,
  reduceState
} from '../src/renderer/jobs/projection.js'
import type { JobState, ProgressEvent } from '../src/shared/jobs-protocol.js'

function ev(kind: string, ts: number, data: Record<string, unknown> = {}): ProgressEvent {
  return { kind, ts, data }
}

describe('projection: phase/scheduled/estimate bookkeeping', () => {
  it('tracks phase and total from phase/scheduled events', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('phase', 100, { phase: 'world' }))
    expect(s.phase).toBe('world')
    expect(s.status).toBe('running')
    s = reduceEvent(s, ev('scheduled', 101, { artifacts_total: 16, events: 90 }))
    expect(s.total).toBe(16)
  })

  it('records the D13 per-artifact cost estimate', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('estimate', 100, { estimated_cost_usd: 1.008, artifacts_total: 16, model: 'claude-opus-4-8' }))
    expect(s.estimatedCostPerArtifact).toBeCloseTo(1.008 / 16)
    expect(s.model).toBe('claude-opus-4-8')
  })
})

describe('projection: artifact events drive done/cached/cost and ETA', () => {
  it('increments done/cached and tracks costSoFar from cost_usd_total', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('scheduled', 0, { artifacts_total: 4 }))
    s = reduceEvent(s, ev('artifact', 1, { done: 1, total: 4, cached: false, cost_usd_total: 0.1 }))
    s = reduceEvent(s, ev('artifact', 2, { done: 2, total: 4, cached: true, cost_usd_total: 0.1 }))
    expect(s.done).toBe(2)
    expect(s.cached).toBe(1)
    expect(s.costSoFar).toBeCloseTo(0.1)
  })

  it('falls back to the D13 per-artifact estimate for cost projection until 3 real artifacts land', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('estimate', 0, { estimated_cost_usd: 1.0, artifacts_total: 10 }))
    s = reduceEvent(s, ev('scheduled', 0, { artifacts_total: 10 }))
    // First real artifact: only 1 sample so far -> still the D13 fallback.
    s = reduceEvent(s, ev('artifact', 1, { done: 1, total: 10, cached: false, cost_usd_total: 0.05 }))
    expect(s.realArtifactCount).toBe(1)
    // projected = costSoFar + (10-1) * (1.0/10) = 0.05 + 0.9 = 0.95
    expect(s.projectedTotalCost).toBeCloseTo(0.05 + 9 * 0.1)
  })

  it('switches to the observed mean once 3 real artifacts have landed', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('estimate', 0, { estimated_cost_usd: 1.0, artifacts_total: 10 }))
    s = reduceEvent(s, ev('scheduled', 0, { artifacts_total: 10 }))
    s = reduceEvents(s, [
      ev('artifact', 1, { done: 1, total: 10, cached: false, cost_usd_total: 0.3 }),
      ev('artifact', 2, { done: 2, total: 10, cached: false, cost_usd_total: 0.6 }),
      ev('artifact', 3, { done: 3, total: 10, cached: false, cost_usd_total: 0.9 })
    ])
    expect(s.realArtifactCount).toBe(3)
    // observed mean cost per non-cached artifact = 0.9 / 3 = 0.3, remaining 7
    expect(s.projectedTotalCost).toBeCloseTo(0.9 + 7 * 0.3)
  })

  it('does not let a cached artifact feed the non-cached seconds EWMA', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('scheduled', 0, { artifacts_total: 3 }))
    s = reduceEvent(s, ev('artifact', 10, { done: 1, total: 3, cached: false, cost_usd_total: 0.1 }))
    const afterFirst = s.meanSecondsPerArtifact
    // A cached artifact arriving instantly afterwards must not move the mean.
    s = reduceEvent(s, ev('artifact', 10.001, { done: 2, total: 3, cached: true, cost_usd_total: 0.1 }))
    expect(s.meanSecondsPerArtifact).toBe(afterFirst)
    expect(s.realArtifactCount).toBe(1)
  })

  it('computes remainingSeconds from the EWMA and the remaining artifact count', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('scheduled', 0, { artifacts_total: 5 }))
    // Two real artifacts, 2 seconds apart each -> EWMA converges toward 2s.
    s = reduceEvent(s, ev('artifact', 2, { done: 1, total: 5, cached: false, cost_usd_total: 0.1 }))
    s = reduceEvent(s, ev('artifact', 4, { done: 2, total: 5, cached: false, cost_usd_total: 0.2 }))
    expect(s.meanSecondsPerArtifact).not.toBeNull()
    expect(s.remainingSeconds).toBeCloseTo((5 - 2) * (s.meanSecondsPerArtifact as number))
  })
})

describe('projection: terminal events', () => {
  it('a paused event sets status/phase to paused', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('paused', 5, { done: 2, total: 5 }))
    expect(s.status).toBe('paused')
    expect(s.phase).toBe('paused')
  })

  it('an error event carries message/type and marks the job failed', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('error', 5, { message: 'ceiling exceeded', type: 'CostCeilingExceeded' }))
    expect(s.status).toBe('failed')
    expect(s.error).toEqual({ message: 'ceiling exceeded', type: 'CostCeilingExceeded' })
  })

  it('a done event sets run_id/run_dir, zeroes remainingSeconds, and finalizes cost', () => {
    let s = initProjection()
    s = reduceEvent(
      s,
      ev('done', 5, { run_id: 'r1', run_dir: '/runs/r1', artifacts: 4, events: 4, cost_usd_total: 0.42 })
    )
    expect(s.status).toBe('done')
    expect(s.runId).toBe('r1')
    expect(s.runDir).toBe('/runs/r1')
    expect(s.remainingSeconds).toBe(0)
    expect(s.costSoFar).toBeCloseTo(0.42)
    expect(s.projectedTotalCost).toBeCloseTo(0.42)
  })

  it('the log tail keeps every event up to a cap, most recent last', () => {
    let s = initProjection()
    for (let i = 0; i < 5; i++) s = reduceEvent(s, ev('artifact', i, { done: i, total: 10 }))
    expect(s.log).toHaveLength(5)
    expect(s.log[s.log.length - 1].data.done).toBe(4)
  })
})

describe('projection: reduceState (authoritative terminal state.json)', () => {
  function fakeJobState(overrides: Partial<JobState> = {}): JobState {
    return {
      status: 'done',
      pid: null,
      run_id: 'r1',
      run_dir: '/runs/r1',
      phase: 'done',
      artifacts_done: 4,
      artifacts_total: 4,
      artifacts_cached: 1,
      estimate: null,
      cost_usd_total: 0.5,
      cost_usd_segment: 0.5,
      usage_total: {},
      segments: [],
      started_at: null,
      updated_at: null,
      finished_at: null,
      error: null,
      ...overrides
    }
  }

  it('merges status/counters/cost from the terminal state.json onto a live projection', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('scheduled', 0, { artifacts_total: 4 }))
    s = reduceState(s, fakeJobState())
    expect(s.status).toBe('done')
    expect(s.done).toBe(4)
    expect(s.cached).toBe(1)
    expect(s.costSoFar).toBeCloseTo(0.5)
    expect(s.remainingSeconds).toBe(0)
  })

  it('a null state is a no-op', () => {
    const s = initProjection()
    expect(reduceState(s, null)).toBe(s)
  })

  it('carries the failure through from state.json', () => {
    let s = initProjection()
    s = reduceState(
      s,
      fakeJobState({ status: 'failed', error: { message: 'boom', type: 'CostCeilingExceeded' } })
    )
    expect(s.status).toBe('failed')
    expect(s.error?.type).toBe('CostCeilingExceeded')
  })
})

describe('projection: elapsedSeconds', () => {
  it('is 0 before any event has been applied', () => {
    expect(elapsedSeconds(initProjection(), 999_999)).toBe(0)
  })

  it('grows with the injected "now" while running', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('phase', 100, { phase: 'world' })) // ts is seconds -> startedAtMs = 100_000
    expect(elapsedSeconds(s, 100_000 + 5_000)).toBeCloseTo(5)
  })

  it('freezes at the last event once the job has finished', () => {
    let s = initProjection()
    s = reduceEvent(s, ev('phase', 100, { phase: 'world' }))
    s = reduceEvent(s, ev('done', 130, { run_id: 'r', run_dir: '/r', cost_usd_total: 0 }))
    const frozen = elapsedSeconds(s, 100_000 + 999_000)
    expect(frozen).toBeCloseTo(30)
  })
})

