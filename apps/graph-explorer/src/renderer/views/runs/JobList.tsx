import { useRunsStore } from '../../store-runs.js'
import type { JobSummary } from '../../../shared/jobs-protocol.js'

function statusLabel(job: JobSummary): string {
  if (job.state.status === 'running' && !job.alive) return 'interrupted'
  return job.state.status
}

function pct(job: JobSummary): number {
  const { artifacts_done, artifacts_total } = job.state
  return artifacts_total > 0 ? Math.round((artifacts_done / artifacts_total) * 100) : 0
}

interface Props {
  onNewRun: () => void
  onExtend: (runPath: string) => void
}

export function JobList({ onNewRun, onExtend }: Props): JSX.Element {
  const jobs = useRunsStore((s) => s.jobs)
  const selectedJobDir = useRunsStore((s) => s.selectedJobDir)
  const selectJob = useRunsStore((s) => s.selectJob)
  const pauseJob = useRunsStore((s) => s.pauseJob)
  const resumeJob = useRunsStore((s) => s.resumeJob)
  const cancelJob = useRunsStore((s) => s.cancelJob)
  const deleteJob = useRunsStore((s) => s.deleteJob)
  const openInExplore = useRunsStore((s) => s.openInExplore)

  return (
    <div className="runs-list">
      <div className="runs-list-head">
        <div className="section-title">Jobs</div>
        <button className="btn primary small" onClick={onNewRun}>
          + New run
        </button>
      </div>
      {jobs.length === 0 && <div className="muted small runs-empty">No runs yet. Start one above.</div>}
      <div className="runs-rows">
        {jobs.map((job) => {
          const status = statusLabel(job)
          const running = job.state.status === 'running' && job.alive
          const resumable = status === 'paused' || status === 'interrupted' || status === 'failed'
          return (
            <div
              key={job.job_dir}
              className={`runs-row ${job.job_dir === selectedJobDir ? 'active' : ''}`}
              onClick={() => selectJob(job.job_dir)}
            >
              <div className="runs-row-head">
                <span className={`status-pill status-${status}`}>{status}</span>
                <span className="runs-job-id mono small">{job.job_id}</span>
              </div>
              <div className="runs-row-bar">
                <div className="runs-progress">
                  <div
                    className="runs-progress-cached"
                    style={{ width: `${job.state.artifacts_total ? (job.state.artifacts_cached / job.state.artifacts_total) * 100 : 0}%` }}
                  />
                  <div className="runs-progress-done" style={{ width: `${pct(job)}%` }} />
                </div>
                <span className="muted small">
                  {job.state.artifacts_done}/{job.state.artifacts_total || '?'}
                </span>
              </div>
              <div className="runs-row-foot">
                <span className="muted small">${job.state.cost_usd_total.toFixed(2)}</span>
                {job.job.kind === 'extend' && <span className="muted small">extend</span>}
                <span className="spacer" />
                <div className="runs-row-actions" onClick={(e) => e.stopPropagation()}>
                  {running && (
                    <button className="mini-btn" onClick={() => void pauseJob(job.job_dir)}>
                      Pause
                    </button>
                  )}
                  {resumable && (
                    <button className="mini-btn" onClick={() => void resumeJob(job.job_dir)}>
                      Resume
                    </button>
                  )}
                  {(running || status === 'paused') && (
                    <button className="mini-btn" onClick={() => void cancelJob(job.job_dir)}>
                      Cancel
                    </button>
                  )}
                  {status === 'done' && job.state.run_dir && (
                    <>
                      <button className="mini-btn" onClick={() => onExtend(job.state.run_dir as string)}>
                        Extend
                      </button>
                      <button className="mini-btn" onClick={() => openInExplore(job.state.run_dir as string)}>
                        Open in Explore
                      </button>
                    </>
                  )}
                  {!running && (
                    <button
                      className="mini-btn"
                      onClick={() => {
                        if (window.confirm(`Delete job ${job.job_id}? The run it produced (if any) is kept.`)) {
                          void deleteJob(job.job_dir)
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
