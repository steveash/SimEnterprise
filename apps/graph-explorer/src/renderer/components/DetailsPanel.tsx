import { useEffect, useState } from 'react'
import { useStore } from '../store.js'
import { typeColor } from '../constants.js'
import type { KGNode } from '../../shared/model.js'

interface Prov {
  nodeId: string
  label: string
  mentions: { artifact_path: string; surface_form: string; line: number; snippet: string | null }[]
  artifacts: string[]
}

export function DetailsPanel(): JSX.Element {
  const selectedId = useStore((s) => s.selectedId)
  const model = useStore((s) => s.model)
  const runPath = useStore((s) => s.runPath)
  const setHighlight = useStore((s) => s.setHighlight)
  const focusNodes = useStore((s) => s.focusNodes)
  const select = useStore((s) => s.select)

  const [depth, setDepth] = useState(1)
  const [prov, setProv] = useState<Prov | null>(null)
  const [artifact, setArtifact] = useState<{ path: string; text: string } | null>(null)
  const [pathInfo, setPathInfo] = useState<string | null>(null)

  const node: KGNode | undefined = model?.nodes.find((n) => n.id === selectedId)

  useEffect(() => {
    setProv(null)
    setArtifact(null)
    setPathInfo(null)
    if (!selectedId || !runPath) return
    const rpc = useStore.getState().rpc
    rpc?.call<Prov>('provenance', { runPath, id: selectedId }).then(setProv).catch(() => {})
  }, [selectedId, runPath])

  if (!node) {
    return (
      <div className="details-empty muted">
        Select a node (click in the graph or a search result) to see its properties, provenance, and connections.
      </div>
    )
  }

  const showNeighbors = async () => {
    const rpc = useStore.getState().rpc
    if (!rpc || !runPath) return
    const sub = await rpc.call<{ nodeIds: string[]; edgeIds: string[] }>('neighbors', {
      runPath,
      id: node.id,
      depth,
      direction: 'both'
    })
    setHighlight(sub.nodeIds, sub.edgeIds)
    focusNodes(sub.nodeIds, true)
  }

  const findPathTo = async (target: string) => {
    const rpc = useStore.getState().rpc
    if (!rpc || !runPath) return
    const p = await rpc.call<{ nodeIds: string[]; edgeIds: string[] } | null>('shortestPath', {
      runPath,
      from: node.id,
      to: target
    })
    if (!p) {
      setPathInfo('no path found')
      return
    }
    setHighlight(p.nodeIds, p.edgeIds)
    focusNodes(p.nodeIds, true)
    setPathInfo(`${p.edgeIds.length} hops`)
  }

  const openArtifact = async (path: string) => {
    const rpc = useStore.getState().rpc
    if (!rpc || !runPath) return
    const res = await rpc.call<{ text: string | null }>('readArtifact', { runPath, path })
    setArtifact({ path, text: res.text ?? '(could not read)' })
  }

  return (
    <div className="details">
      <div className="details-head">
        <span className="type-dot lg" style={{ background: typeColor(node.type) }} />
        <div>
          <div className="details-title">{node.label}</div>
          <div className="details-id mono">{node.id}</div>
        </div>
      </div>

      <div className="btn-row">
        <button className="btn" onClick={showNeighbors}>
          Neighbors
        </button>
        <input
          className="depth-input"
          type="number"
          min={1}
          max={6}
          value={depth}
          onChange={(e) => setDepth(Number(e.target.value))}
          title="depth"
        />
        <button className="btn" onClick={() => focusNodes([node.id], true)}>
          Focus
        </button>
      </div>

      <details className="block">
        <summary>Properties</summary>
        <table className="prop-table">
          <tbody>
            <tr>
              <td className="pk">type</td>
              <td>{node.type}</td>
            </tr>
            {node.aliases.length > 0 && (
              <tr>
                <td className="pk">aliases</td>
                <td>{node.aliases.join(', ')}</td>
              </tr>
            )}
            {Object.entries(node.props).map(([k, v]) => (
              <tr key={k}>
                <td className="pk">{k}</td>
                <td className="pv">{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>

      <details className="block" open>
        <summary>Provenance {prov ? `(${prov.mentions.length} mentions)` : ''}</summary>
        {!prov && <div className="muted small">loading…</div>}
        {prov && prov.mentions.length === 0 && <div className="muted small">no source mentions for this node</div>}
        {prov?.mentions.map((m, i) => (
          <div key={i} className="mention" onClick={() => void openArtifact(m.artifact_path)}>
            <div className="mention-snippet mono">{m.snippet ?? m.surface_form}</div>
            <div className="mention-src muted">
              {m.artifact_path.split('/').pop()} · line {m.line}
            </div>
          </div>
        ))}
      </details>

      {pathInfo && <div className="muted small">path: {pathInfo}</div>}

      {artifact && (
        <div className="block artifact-view">
          <div className="artifact-head">
            <span className="mono small">{artifact.path}</span>
            <button className="mini-btn" onClick={() => setArtifact(null)}>
              close
            </button>
          </div>
          <pre className="artifact-text">{artifact.text}</pre>
        </div>
      )}

      <div className="hint muted small">
        Tip: click a search result then use it as a path target by selecting another node and choosing it below.
      </div>
      <PathPicker onPick={findPathTo} excludeId={node.id} />
      <div className="btn-row">
        <button className="btn ghost" onClick={() => select(null)}>
          Clear selection
        </button>
      </div>
    </div>
  )
}

function PathPicker({ onPick, excludeId }: { onPick: (id: string) => void; excludeId: string }): JSX.Element {
  const model = useStore((s) => s.model)
  const [q, setQ] = useState('')
  if (!model) return <></>
  const matches = q.trim()
    ? model.nodes.filter((n) => n.id !== excludeId && n.label.toLowerCase().includes(q.toLowerCase())).slice(0, 6)
    : []
  return (
    <div className="path-picker">
      <input className="search-input small" placeholder="shortest path to…" value={q} onChange={(e) => setQ(e.target.value)} />
      {matches.map((m) => (
        <div
          key={m.id}
          className="hit-row"
          onClick={() => {
            onPick(m.id)
            setQ('')
          }}
        >
          <span className="hit-label">{m.label}</span>
          <span className="hit-type">{m.type}</span>
        </div>
      ))}
    </div>
  )
}
