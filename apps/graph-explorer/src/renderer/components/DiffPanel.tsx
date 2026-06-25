import { useState } from 'react'
import { useStore } from '../store.js'

export function DiffPanel(): JSX.Element {
  const runs = useStore((s) => s.runs)
  const runPath = useStore((s) => s.runPath)
  const diff = useStore((s) => s.diff)
  const runDiff = useStore((s) => s.runDiff)
  const setHighlight = useStore((s) => s.setHighlight)
  const model = useStore((s) => s.model)
  const [other, setOther] = useState('')

  const others = runs.filter((r) => r.runPath !== runPath)

  const labelFor = (id: string): string => model?.nodes.find((n) => n.id === id)?.label ?? id

  return (
    <div className="diff-panel">
      <div className="muted small">Compare the current run against another to see structural drift.</div>
      <div className="btn-row">
        <select className="model-select grow" value={other} onChange={(e) => setOther(e.target.value)}>
          <option value="">select a run…</option>
          {others.map((r) => (
            <option key={r.runPath} value={r.runPath}>
              {r.runId} ({r.nodeCount}n/{r.edgeCount}e)
            </option>
          ))}
        </select>
        <button className="btn primary" disabled={!other} onClick={() => void runDiff(other)}>
          Diff
        </button>
      </div>

      {diff && (
        <div className="diff-result">
          <div className="diff-summary">
            <span className="added">+{diff.nodes.added.length} nodes</span>
            <span className="removed">−{diff.nodes.removed.length} nodes</span>
            <span className="muted">{diff.nodes.common.length} common</span>
          </div>
          <div className="diff-summary">
            <span className="added">+{diff.edges.added.length} edges</span>
            <span className="removed">−{diff.edges.removed.length} edges</span>
          </div>
          <button
            className="btn ghost"
            onClick={() => setHighlight([...diff.nodes.added, ...diff.nodes.removed])}
          >
            Highlight changed nodes
          </button>
          {diff.nodes.removed.length > 0 && (
            <details className="block" open>
              <summary>Removed in other ({diff.nodes.removed.length})</summary>
              {diff.nodes.removed.slice(0, 40).map((id) => (
                <div key={id} className="diff-row removed mono small" onClick={() => setHighlight([id])}>
                  {labelFor(id)}
                </div>
              ))}
            </details>
          )}
          {diff.nodes.added.length > 0 && (
            <details className="block">
              <summary>Only in other ({diff.nodes.added.length})</summary>
              {diff.nodes.added.slice(0, 40).map((id) => (
                <div key={id} className="diff-row added mono small">
                  {id}
                </div>
              ))}
            </details>
          )}
        </div>
      )}
    </div>
  )
}
