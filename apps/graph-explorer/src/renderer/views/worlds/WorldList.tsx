// Left pane of the Worlds view: EVERY world under the worlds root — built from
// this app or from the CLI — with its status, progress, cost and what it holds
// (graph / data scenarios / eval questions). Same list as the topbar picker.
import { useStore } from '../../store.js'
import { useWorldsStore } from '../../store-worlds.js'
import type { WorldSummary } from '../../../shared/worlds-protocol.js'

function pct(world: WorldSummary): number {
  const { artifacts_done, artifacts_total } = world.job
  return artifacts_total > 0 ? Math.round((artifacts_done / artifacts_total) * 100) : 0
}

interface Props {
  onNewWorld: () => void
  onExtend: (worldDir: string) => void
}

export function WorldList({ onNewWorld, onExtend }: Props): JSX.Element {
  const worlds = useStore((s) => s.worlds)
  const selectedWorldDir = useWorldsStore((s) => s.selectedWorldDir)
  const selectWorld = useWorldsStore((s) => s.selectWorld)
  const pauseWorld = useWorldsStore((s) => s.pauseWorld)
  const resumeWorld = useWorldsStore((s) => s.resumeWorld)
  const cancelWorld = useWorldsStore((s) => s.cancelWorld)
  const deleteWorld = useWorldsStore((s) => s.deleteWorld)
  const openInExplore = useWorldsStore((s) => s.openInExplore)

  return (
    <div className="runs-list">
      <div className="runs-list-head">
        <div className="section-title">Worlds</div>
        <button className="btn primary small" onClick={onNewWorld}>
          + New world
        </button>
      </div>
      {worlds.length === 0 && (
        <div className="muted small runs-empty">
          No worlds under the worlds root yet. Start one above, or build one from the command line with{' '}
          <code>enterprise-sim world create &lt;config&gt;</code> — it will appear here.
        </div>
      )}
      <div className="runs-rows">
        {worlds.map((world) => {
          const status = world.status
          const running = status === 'running'
          const resumable = status === 'paused' || status === 'interrupted' || status === 'failed' || status === 'created'
          const dataCount = Object.keys(world.data).length
          const lastKind = world.history[world.history.length - 1]?.kind
          return (
            <div
              key={world.world_dir}
              className={`runs-row ${world.world_dir === selectedWorldDir ? 'active' : ''}`}
              onClick={() => selectWorld(world.world_dir)}
            >
              <div className="runs-row-head">
                <span className={`status-pill status-${status}`}>{status}</span>
                <span className="runs-job-id mono small" title={world.world_dir}>
                  {world.world_id}
                </span>
              </div>
              <div className="world-meta small muted">
                {world.company.name}
                {world.window.start && world.window.end ? ` · ${world.window.start} → ${world.window.end}` : ''}
                {world.live ? ' · live' : ''}
              </div>
              <div className="runs-row-bar">
                <div className="runs-progress">
                  <div
                    className="runs-progress-cached"
                    style={{
                      width: `${world.job.artifacts_total ? (world.job.artifacts_cached / world.job.artifacts_total) * 100 : 0}%`
                    }}
                  />
                  <div className="runs-progress-done" style={{ width: `${pct(world)}%` }} />
                </div>
                <span className="muted small">
                  {world.job.artifacts_done}/{world.job.artifacts_total || '?'}
                </span>
              </div>
              <div className="world-badges">
                <span className={`eval-badge ${world.has_kg ? 'ok' : 'muted'}`} title="gold knowledge graph exported">
                  {world.has_kg ? `graph ${world.counts.nodes ?? '?'}n/${world.counts.edges ?? '?'}e` : 'no graph yet'}
                </span>
                {dataCount > 0 && (
                  <span className="eval-badge ok" title={Object.keys(world.data).join(', ')}>
                    data ×{dataCount}
                  </span>
                )}
                {typeof world.evals.questions === 'number' && (
                  <span className="eval-badge muted">evals {world.evals.questions}</span>
                )}
                {lastKind === 'extend' && <span className="eval-badge muted">extended</span>}
                {world.lineage && <span className="eval-badge muted">fork</span>}
              </div>
              <div className="runs-row-foot">
                <span className="muted small">${world.job.cost_usd_total.toFixed(2)}</span>
                <span className="spacer" />
                <div className="runs-row-actions" onClick={(e) => e.stopPropagation()}>
                  {running && (
                    <button className="mini-btn" onClick={() => void pauseWorld(world.world_dir)}>
                      Pause
                    </button>
                  )}
                  {resumable && (
                    <button className="mini-btn" onClick={() => void resumeWorld(world.world_dir)}>
                      {status === 'created' ? 'Build' : 'Resume'}
                    </button>
                  )}
                  {(running || status === 'paused') && (
                    <button className="mini-btn" onClick={() => void cancelWorld(world.world_dir)}>
                      Cancel
                    </button>
                  )}
                  {status === 'done' && (
                    <button className="mini-btn" onClick={() => onExtend(world.world_dir)}>
                      Extend
                    </button>
                  )}
                  {world.has_kg && (
                    <button className="mini-btn" onClick={() => openInExplore(world.world_dir)}>
                      Open in Explore
                    </button>
                  )}
                  {!running && (
                    <button
                      className="mini-btn"
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete world ${world.world_id}? This removes the whole folder — corpus, graph, data, cache.`
                          )
                        ) {
                          void deleteWorld(world.world_dir)
                        }
                      }}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
