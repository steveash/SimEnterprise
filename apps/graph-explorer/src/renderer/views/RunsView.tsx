// The Runs view (docs/EXPLORER_RUNS.md §5): create/extend runs from a form,
// watch progress live, pause/resume/cancel. Own store (`../store-runs.js`),
// own stylesheet (`./runs.css`) — kept separate from the Explore view's.
import { useEffect, useState } from 'react'
import { useRunsStore } from '../store-runs.js'
import { JobList } from './runs/JobList.js'
import { NewRunForm } from './runs/NewRunForm.js'
import { JobDetail } from './runs/JobDetail.js'
import './runs.css'

type RightPane = 'empty' | 'form' | 'detail'

export function RunsView(): JSX.Element {
  const init = useRunsStore((s) => s.init)
  const selectedJobDir = useRunsStore((s) => s.selectedJobDir)
  const selectJob = useRunsStore((s) => s.selectJob)
  const resetForm = useRunsStore((s) => s.resetForm)
  const startExtend = useRunsStore((s) => s.startExtend)
  const [pane, setPane] = useState<RightPane>('empty')

  useEffect(() => {
    void init()
  }, [init])

  useEffect(() => {
    if (selectedJobDir) setPane('detail')
  }, [selectedJobDir])

  const openNewRun = (): void => {
    selectJob(null)
    resetForm()
    setPane('form')
  }

  const openExtend = (runPath: string): void => {
    selectJob(null)
    resetForm()
    void startExtend(runPath).then(() => setPane('form'))
  }

  return (
    <div className="view view-runs">
      <aside className="runs-sidebar">
        <JobList onNewRun={openNewRun} onExtend={openExtend} />
      </aside>
      <main className="runs-main">
        {pane === 'form' && <NewRunForm onDone={() => setPane(useRunsStore.getState().selectedJobDir ? 'detail' : 'empty')} />}
        {pane === 'detail' && selectedJobDir && <JobDetail />}
        {pane === 'empty' && (
          <div className="view-empty">
            <div className="view-empty-title">Runs</div>
            <div className="muted">Pick a job on the left, or start a new run.</div>
          </div>
        )}
      </main>
    </div>
  )
}
