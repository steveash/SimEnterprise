import { useState } from 'react'
import { useStore } from '../store.js'
import type { DiffEdge, DiffNode, TypeDelta } from '../../shared/protocol.js'

export function DiffPanel(): JSX.Element {
  const runs = useStore((s) => s.runs)
  const runPath = useStore((s) => s.runPath)
  const diff = useStore((s) => s.diff)
  const runDiff = useStore((s) => s.runDiff)
  const select = useStore((s) => s.select)
  const setHighlight = useStore((s) => s.setHighlight)
  const focusNodes = useStore((s) => s.focusNodes)
  const [other, setOther] = useState('')

  const others = runs.filter((r) => r.runPath !== runPath)

  // Reveal a changed node on the graph: select + highlight + focus.
  // (Items only in the other run aren't in the current graph, so focus is a no-op there.)
  const revealNode = (n: DiffNode): void => {
    select(n.id)
    setHighlight([n.id])
    focusNodes([n.id])
  }
  // Reveal a changed edge by highlighting it plus both endpoints.
  const revealEdge = (e: DiffEdge): void => {
    select(null)
    setHighlight([e.src, e.dst], [e.id])
    focusNodes([e.src, e.dst])
  }

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
          <div className="diff-header">
            <span className="mono small">{diff.a.runId}</span>
            <span className="muted"> vs </span>
            <span className="mono small">{diff.b.runId}</span>
            <span className="diff-counts">
              <span className="added">+{diff.nodes.added.length}</span>
              <span className="removed">−{diff.nodes.removed.length}</span>
              <span className="muted">n</span>
              <span className="added">+{diff.edges.added.length}</span>
              <span className="removed">−{diff.edges.removed.length}</span>
              <span className="muted">e</span>
            </span>
          </div>

          <button
            className="btn ghost"
            onClick={() => setHighlight([...diff.nodes.added, ...diff.nodes.removed])}
          >
            Highlight changed nodes
          </button>

          <Breakdown title="Nodes by type" rows={diff.nodeTypeDeltas} />
          <Breakdown title="Edges by type" rows={diff.edgeTypeDeltas} />

          <NodeList
            title={`Removed in ${diff.b.runId} (${diff.nodeChanges.removed.length})`}
            cls="removed"
            items={diff.nodeChanges.removed}
            onPick={revealNode}
            open
          />
          <NodeList
            title={`Only in ${diff.b.runId} (${diff.nodeChanges.added.length})`}
            cls="added"
            items={diff.nodeChanges.added}
            onPick={revealNode}
          />
          <EdgeList
            title={`Edges removed in ${diff.b.runId} (${diff.edgeChanges.removed.length})`}
            cls="removed"
            items={diff.edgeChanges.removed}
            onPick={revealEdge}
          />
          <EdgeList
            title={`Edges only in ${diff.b.runId} (${diff.edgeChanges.added.length})`}
            cls="added"
            items={diff.edgeChanges.added}
            onPick={revealEdge}
          />
        </div>
      )}
    </div>
  )
}

function Breakdown({ title, rows }: { title: string; rows: TypeDelta[] }): JSX.Element | null {
  if (rows.length === 0) return null
  return (
    <details className="block" open>
      <summary>
        {title} ({rows.length})
      </summary>
      <table className="prop-table">
        <tbody>
          {rows.map((r) => (
            <tr key={r.type}>
              <td className="pk">{r.type}</td>
              <td className="pv">
                <span className="added">+{r.added}</span> <span className="removed">−{r.removed}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  )
}

function NodeList({
  title,
  cls,
  items,
  onPick,
  open = false
}: {
  title: string
  cls: string
  items: DiffNode[]
  onPick: (n: DiffNode) => void
  open?: boolean
}): JSX.Element | null {
  if (items.length === 0) return null
  return (
    <details className="block" open={open}>
      <summary>{title}</summary>
      {items.slice(0, 60).map((n) => (
        <div key={n.id} className={`diff-row ${cls} small`} onClick={() => onPick(n)} title={n.id}>
          <span className="diff-type">{n.type}</span> {n.label}
        </div>
      ))}
      {items.length > 60 && <div className="muted small">…and {items.length - 60} more</div>}
    </details>
  )
}

function EdgeList({
  title,
  cls,
  items,
  onPick,
  open = false
}: {
  title: string
  cls: string
  items: DiffEdge[]
  onPick: (e: DiffEdge) => void
  open?: boolean
}): JSX.Element | null {
  if (items.length === 0) return null
  return (
    <details className="block" open={open}>
      <summary>{title}</summary>
      {items.slice(0, 60).map((e) => (
        <div key={e.id} className={`diff-row ${cls} small`} onClick={() => onPick(e)} title={e.id}>
          <span className="diff-type">{e.type}</span> {e.srcLabel} → {e.dstLabel}
        </div>
      ))}
      {items.length > 60 && <div className="muted small">…and {items.length - 60} more</div>}
    </details>
  )
}
