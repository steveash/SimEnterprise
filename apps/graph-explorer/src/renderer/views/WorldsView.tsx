// The Worlds view (docs/EXPLORER_RUNS.md §5): every world on disk in one list,
// create / extend worlds from a form, watch a build live, pause/resume/cancel.
// Own store (`../store-worlds.js`) for the form + build projection; the list
// itself is the main store's (`useStore().worlds`) so it is the same list the
// topbar picker shows. Own stylesheet (`./worlds.css`).
import { useEffect, useState } from 'react'
import { useWorldsStore } from '../store-worlds.js'
import { WorldList } from './worlds/WorldList.js'
import { NewWorldForm } from './worlds/NewWorldForm.js'
import { WorldDetail } from './worlds/WorldDetail.js'
import './worlds.css'

type RightPane = 'empty' | 'form' | 'detail' | 'extend-loading' | 'extend-error'

export function WorldsView(): JSX.Element {
  const init = useWorldsStore((s) => s.init)
  const selectedWorldDir = useWorldsStore((s) => s.selectedWorldDir)
  const selectWorld = useWorldsStore((s) => s.selectWorld)
  const resetForm = useWorldsStore((s) => s.resetForm)
  const startExtend = useWorldsStore((s) => s.startExtend)
  const [pane, setPane] = useState<RightPane>('empty')
  const [extendTarget, setExtendTarget] = useState<string | null>(null)
  const [extendError, setExtendError] = useState<string | null>(null)

  useEffect(() => {
    void init()
  }, [init])

  useEffect(() => {
    if (selectedWorldDir) setPane('detail')
  }, [selectedWorldDir])

  const openNewWorld = (): void => {
    selectWorld(null)
    resetForm()
    setPane('form')
  }

  const openExtend = (worldDir: string): void => {
    // Never leave the pane blank while the world's config loads (or if it fails).
    selectWorld(null)
    resetForm()
    setExtendTarget(worldDir)
    setExtendError(null)
    setPane('extend-loading')
    startExtend(worldDir).then(
      () => setPane('form'),
      (e: Error) => {
        setExtendError(e.message)
        setPane('extend-error')
      }
    )
  }

  return (
    <div className="view view-runs">
      <aside className="runs-sidebar">
        <WorldList onNewWorld={openNewWorld} onExtend={openExtend} />
      </aside>
      <main className="runs-main">
        {pane === 'form' && (
          <NewWorldForm onDone={() => setPane(useWorldsStore.getState().selectedWorldDir ? 'detail' : 'empty')} />
        )}
        {pane === 'detail' && selectedWorldDir && <WorldDetail />}
        {pane === 'extend-loading' && (
          <div className="view-empty">
            <span className="spinner lg" aria-hidden="true" />
            <div className="muted" style={{ marginTop: 10 }}>
              Loading <span className="mono">{extendTarget?.split('/').pop()}</span>'s config…
            </div>
          </div>
        )}
        {pane === 'extend-error' && (
          <div className="view-empty">
            <div className="view-empty-title">Couldn't open Extend</div>
            <div className="msg-error">⚠ {extendError}</div>
            <div className="btn-row" style={{ justifyContent: 'center', marginTop: 10 }}>
              <button className="btn primary" onClick={() => extendTarget && openExtend(extendTarget)}>
                Retry
              </button>
              <button className="btn ghost" onClick={() => setPane('empty')}>
                Dismiss
              </button>
            </div>
          </div>
        )}
        {pane === 'empty' && (
          <div className="view-empty">
            <div className="view-empty-title">Worlds</div>
            <div className="muted world-explainer">
              <p>
                A <strong>world</strong> is one simulated company: one folder on disk holding its document corpus, its
                gold knowledge graph, and any structured data products, described by a single <code>.world</code>{' '}
                file. Everything in this app works on worlds — Explore browses one, Evals tests one, Templates add
                the departments and playbooks a world is built from.
              </p>
              <p>
                Pick a world on the left to see how it was built and drive it (pause, resume, extend), or start a new
                one. Worlds created from the command line show up here too.
              </p>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
