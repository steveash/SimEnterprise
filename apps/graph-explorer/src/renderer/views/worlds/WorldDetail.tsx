import { useEffect, useState } from 'react'
import { useStore } from '../../store.js'
import { useWorldsStore } from '../../store-worlds.js'
import { elapsedSeconds, type JobPhase } from '../../worlds/projection.js'

const PHASES: JobPhase[] = ['world', 'schedule', 'estimate', 'render', 'assemble', 'data', 'evals', 'done']

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

function describeChange(changes: Record<string, unknown> | null | undefined): string {
  if (!changes) return ''
  const parts: string[] = []
  const end = changes.period_end
  if (Array.isArray(end) && end.length === 2 && end[0] !== end[1]) parts.push(`window end ${end[0]} → ${end[1]}`)
  const added = changes.added_projects
  if (Array.isArray(added) && added.length) parts.push(`added ${added.join(', ')}`)
  return parts.length ? ` — ${parts.join('; ')}` : ''
}

export function WorldDetail(): JSX.Element | null {
  const worldDir = useWorldsStore((s) => s.selectedWorldDir)
  const projection = useWorldsStore((s) => s.projection)
  const worlds = useStore((s) => s.worlds)
  const pauseWorld = useWorldsStore((s) => s.pauseWorld)
  const resumeWorld = useWorldsStore((s) => s.resumeWorld)
  const cancelWorld = useWorldsStore((s) => s.cancelWorld)
  const openInExplore = useWorldsStore((s) => s.openInExplore)
  const raiseCeilingAndResume = useWorldsStore((s) => s.raiseCeilingAndResume)
  const [ceilingInput, setCeilingInput] = useState('')
  const [, setTick] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  if (!worldDir) return null
  const world = worlds.find((w) => w.world_dir === worldDir)
  const job = world?.job

  const done = projection.done || job?.artifacts_done || 0
  const total = projection.total || job?.artifacts_total || 0
  const cached = projection.cached || job?.artifacts_cached || 0
  const costSoFar = projection.costSoFar || job?.cost_usd_total || 0
  const status = world ? world.status : projection.status
  const currentPhaseIdx = PHASES.indexOf((job?.phase as JobPhase) ?? projection.phase)

  const doneFraction = total > 0 ? done / total : 0
  const cachedFraction = total > 0 ? cached / total : 0
  const scenarios = world ? Object.entries(world.data) : []
  const errorType = projection.error?.type ?? job?.error?.type

  return (
    <div className="runs-detail">
      <div className="runs-detail-head">
        <span className={`status-pill status-${status}`}>{status}</span>
        <span className="mono">{world?.world_id ?? worldDir}</span>
        <span className="spacer" />
        {status === 'running' && (
          <button className="btn ghost" onClick={() => void pauseWorld(worldDir)}>
            Pause
          </button>
        )}
        {(status === 'paused' || status === 'interrupted' || status === 'failed' || status === 'created') && (
          <button className="btn ghost" onClick={() => void resumeWorld(worldDir)}>
            {status === 'created' ? 'Build' : 'Resume'}
          </button>
        )}
        {(status === 'running' || status === 'paused') && (
          <button className="btn ghost" onClick={() => void cancelWorld(worldDir)}>
            Cancel
          </button>
        )}
        {world?.has_kg && (
          <button className="btn primary" onClick={() => openInExplore(worldDir)}>
            Open in Explore
          </button>
        )}
      </div>

      {world && (
        <div className="muted small world-detail-meta">
          <span className="mono">{world.world_dir}</span>
          <br />
          {world.company.name}
          {world.company.vertical ? ` · ${world.company.vertical}` : ''}
          {world.company.size ? ` · ${world.company.size}` : ''} · seed {world.seed}
          {world.window.start && world.window.end ? ` · ${world.window.start} → ${world.window.end}` : ''} ·{' '}
          {world.live ? `live (${world.render_backend})` : 'deterministic (fake backend)'}
        </div>
      )}

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
        <Stat label="Model" value={projection.model ?? world?.render_backend ?? '—'} />
      </div>

      {(projection.error || (job && status === 'failed')) && (
        <div className="runs-error">
          <div className="msg-error">⚠ {projection.error?.message ?? job?.error?.message ?? 'build failed'}</div>
          {errorType === 'CostCeilingExceeded' && (
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
                onClick={() => void raiseCeilingAndResume(worldDir, Number(ceilingInput))}
              >
                Raise ceiling &amp; resume
              </button>
            </div>
          )}
        </div>
      )}

      {world && (
        <details className="block" open>
          <summary>
            What this world holds ({world.has_kg ? 'graph' : 'no graph'}
            {scenarios.length ? `, ${scenarios.length} data scenario${scenarios.length === 1 ? '' : 's'}` : ''}
            {typeof world.evals.questions === 'number' ? `, ${world.evals.questions} eval questions` : ''})
          </summary>
          <div className="small world-holdings">
            <div>
              <strong>Documents + graph:</strong>{' '}
              {world.has_kg
                ? `${world.counts.nodes ?? '?'} nodes, ${world.counts.edges ?? '?'} edges, ${world.counts.events ?? '?'} events`
                : 'not built yet'}
            </div>
            {scenarios.map(([name, s]) => (
              <div key={name}>
                <strong>data/{name}:</strong> {s.title ?? name} — {s.rows.toLocaleString()} rows across {s.tables} tables,{' '}
                {s.questions.answerable ?? '?'}/{s.questions.total ?? '?'} questions answerable
              </div>
            ))}
            {scenarios.length === 0 && (
              <div className="muted">
                No structured data yet — use <strong>Extend</strong> and tick data scenarios under “Structured data”.
              </div>
            )}
          </div>
        </details>
      )}

      {world && world.history.length > 0 && (
        <details className="block">
          <summary>History ({world.history.length})</summary>
          <div className="small world-history">
            {world.history.map((h, i) => (
              <div key={i}>
                <span className="muted">{h.at ? new Date(h.at).toLocaleString() : '—'}</span> {h.kind}
                {describeChange(h.changes)}
              </div>
            ))}
            {world.lineage?.parent_world_id && (
              <div className="muted">forked from {world.lineage.parent_world_id}</div>
            )}
          </div>
        </details>
      )}

      <details className="block runs-log" open>
        <summary>Log ({projection.log.length})</summary>
        <div className="runs-log-scroll mono small">
          {projection.log.slice(-200).map((e, i) => (
            <div key={i} className="runs-log-line">
              <span className="muted">{new Date(e.ts * 1000).toLocaleTimeString()}</span> {e.kind}{' '}
              {logDetail(e)}
            </div>
          ))}
          {projection.log.length === 0 && <div className="muted">no progress yet</div>}
        </div>
      </details>
    </div>
  )
}

function logDetail(e: { kind: string; data: Record<string, unknown> }): string {
  const d = e.data
  switch (e.kind) {
    case 'artifact':
      return `(${d.done}/${d.total}${d.cached ? ' cached' : ''})`
    case 'phase':
      return String(d.phase ?? '')
    case 'data_scenario':
      return `${d.scenario} (${Number(d.index) + 1}/${d.total})`
    case 'data_done':
      return `${d.scenario}: ${Number(d.rows).toLocaleString()} rows, ${d.questions_answerable}/${d.questions_total} questions answerable`
    case 'warning':
    case 'error':
      return String(d.message ?? '')
    default:
      return ''
  }
}

function Stat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="runs-stat">
      <div className="muted small">{label}</div>
      <div className="runs-stat-value">{value}</div>
    </div>
  )
}
