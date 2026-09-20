import { useEffect, useState } from 'react'
import { useRunsStore } from '../../store-runs.js'
import { elapsedSeconds, type JobPhase } from '../../jobs/projection.js'

const PHASES: JobPhase[] = ['world', 'schedule', 'estimate', 'render', 'assemble', 'evals', 'done']

function fmtSeconds(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return '—'
  const total = Math.round(s)
  const m = Math.floor(total / 60)
  const sec = total % 60
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`
}

function fmtUsd(n: number | null): string {
  return n === null || !Number.isFinite(n) ? '—' : `$${n.toFixed(2)}`
}

export function JobDetail(): JSX.Element | null {
  const jobDir = useRunsStore((s) => s.selectedJobDir)
  const projection = useRunsStore((s) => s.projection)
  const jobs = useRunsStore((s) => s.jobs)
  const pauseJob = useRunsStore((s) => s.pauseJob)
  const resumeJob = useRunsStore((s) => s.resumeJob)
  const cancelJob = useRunsStore((s) => s.cancelJob)
  const openInExplore = useRunsStore((s) => s.openInExplore)
  const raiseCeilingAndResume = useRunsStore((s) => s.raiseCeilingAndResume)
  const [ceilingInput, setCeilingInput] = useState('')
  const [, setTick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  if (!jobDir) return null
  const job = jobs.find((j) => j.job_dir === jobDir)

  const done = projection.done || job?.state.artifacts_done || 0
  const total = projection.total || job?.state.artifacts_total || 0
  const cached = projection.cached || job?.state.artifacts_cached || 0
  const costSoFar = projection.costSoFar || job?.state.cost_usd_total || 0
  const status = job ? (job.state.status === 'running' && !job.alive ? 'interrupted' : job.state.status) : projection.status
  const runDir = projection.runDir ?? job?.state.run_dir ?? null
  const currentPhaseIdx = PHASES.indexOf((job?.state.phase as JobPhase) ?? projection.phase)

  const doneFraction = total > 0 ? done / total : 0
  const cachedFraction = total > 0 ? cached / total : 0

  return (
    <div className="runs-detail">
      <div className="runs-detail-head">
        <span className={`status-pill status-${status}`}>{status}</span>
        <span className="mono">{job?.job_id ?? jobDir}</span>
        <span className="spacer" />
        {status === 'running' && (
          <button className="btn ghost" onClick={() => void pauseJob(jobDir)}>
            Pause
          </button>
        )}
        {(status === 'paused' || status === 'interrupted' || status === 'failed') && (
          <button className="btn ghost" onClick={() => void resumeJob(jobDir)}>
            Resume
          </button>
        )}
        {(status === 'running' || status === 'paused') && (
          <button className="btn ghost" onClick={() => void cancelJob(jobDir)}>
            Cancel
          </button>
        )}
        {status === 'done' && runDir && (
          <button className="btn primary" onClick={() => openInExplore(runDir)}>
            Open in Explore
          </button>
        )}
      </div>

      <div className="runs-phases">
        {PHASES.map((p, i) => (
          <div
            key={p}
            className={`runs-phase ${i < currentPhaseIdx ? 'done' : ''} ${i === currentPhaseIdx ? 'current' : ''}`}
          >
            {p}
          </div>
        ))}
      </div>

      <div className="runs-progress-block">
        <div className="runs-progress lg">
          <div className="runs-progress-cached" style={{ width: `${cachedFraction * 100}%` }} />
          <div className="runs-progress-done" style={{ width: `${doneFraction * 100}%` }} />
        </div>
        <div className="muted small">
          {done}/{total || '?'} artifacts · {cached} cached
        </div>
      </div>

      <div className="runs-stats">
        <Stat label="Elapsed" value={fmtSeconds(elapsedSeconds(projection))} />
        <Stat label="ETA" value={fmtSeconds(projection.remainingSeconds)} />
        <Stat label="Cost so far" value={fmtUsd(costSoFar)} />
        <Stat label="Projected total" value={fmtUsd(projection.projectedTotalCost)} />
        <Stat label="Model" value={projection.model ?? job?.job.kind ?? '—'} />
      </div>

      {(projection.error || (job && status === 'failed')) && (
        <div className="runs-error">
          <div className="msg-error">⚠ {projection.error?.message ?? 'job failed'}</div>
          {projection.error?.type === 'CostCeilingExceeded' && (
            <div className="btn-row">
              <input
                className="search-input small"
                placeholder="new ceiling (USD)"
                value={ceilingInput}
                onChange={(e) => setCeilingInput(e.target.value)}
              />
              <button
                className="btn primary"
                disabled={!ceilingInput || Number.isNaN(Number(ceilingInput))}
                onClick={() => void raiseCeilingAndResume(jobDir, Number(ceilingInput))}
              >
                Raise ceiling &amp; resume
              </button>
            </div>
          )}
        </div>
      )}

      <details className="block runs-log" open>
        <summary>Log ({projection.log.length})</summary>
        <div className="runs-log-scroll mono small">
          {projection.log.slice(-200).map((e, i) => (
            <div key={i} className="runs-log-line">
              <span className="muted">{new Date(e.ts * 1000).toLocaleTimeString()}</span> {e.kind}{' '}
              {e.kind === 'artifact' ? `(${e.data.done}/${e.data.total}${e.data.cached ? ' cached' : ''})` : ''}
            </div>
          ))}
          {projection.log.length === 0 && <div className="muted">no progress yet</div>}
        </div>
      </details>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="runs-stat">
      <div className="muted small">{label}</div>
      <div className="runs-stat-value">{value}</div>
    </div>
  )
}
