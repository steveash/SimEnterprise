import { useStore } from '../store.js'
import { typeColor } from '../constants.js'

export function Legend(): JSX.Element {
  const model = useStore((s) => s.model)
  const visibleTypes = useStore((s) => s.visibleTypes)
  const visibleEdgeTypes = useStore((s) => s.visibleEdgeTypes)
  const toggleType = useStore((s) => s.toggleType)
  const toggleEdgeType = useStore((s) => s.toggleEdgeType)
  if (!model) return <></>

  const counts = new Map<string, number>()
  for (const n of model.nodes) counts.set(n.type, (counts.get(n.type) ?? 0) + 1)
  const edgeCounts = new Map<string, number>()
  for (const e of model.edges) edgeCounts.set(e.type, (edgeCounts.get(e.type) ?? 0) + 1)

  return (
    <div className="panel-section">
      <div className="section-title">Node types</div>
      <div className="legend">
        {model.nodeTypes.map((t) => (
          <label key={t} className={`legend-row ${visibleTypes.has(t) ? '' : 'off'}`}>
            <input type="checkbox" checked={visibleTypes.has(t)} onChange={() => toggleType(t)} />
            <span className="type-dot" style={{ background: typeColor(t) }} />
            <span className="legend-label">{t}</span>
            <span className="legend-count">{counts.get(t)}</span>
          </label>
        ))}
      </div>
      <div className="section-title">Edge types</div>
      <div className="legend edge-legend">
        {model.edgeTypes.map((t) => (
          <label key={t} className={`legend-row ${visibleEdgeTypes.has(t) ? '' : 'off'}`}>
            <input type="checkbox" checked={visibleEdgeTypes.has(t)} onChange={() => toggleEdgeType(t)} />
            <span className="legend-label mono">{t}</span>
            <span className="legend-count">{edgeCounts.get(t)}</span>
          </label>
        ))}
      </div>
    </div>
  )
}
