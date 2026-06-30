// Pure structural diff between two loaded gold KG runs.
//
// Kept engine-free (operates on GraphModel, not LoadedRun) so it is cheap to unit
// test and reusable: the sidecar's `diffRuns` op is a thin wrapper around this.

import type { GraphModel, KGNode, KGEdge } from '../../shared/model.js'
import type { DiffResult, DiffNode, DiffEdge, TypeDelta } from '../../shared/protocol.js'

function partition(x: Set<string>, y: Set<string>): { added: string[]; removed: string[]; common: string[] } {
  return {
    added: [...y].filter((v) => !x.has(v)), // only in B
    removed: [...x].filter((v) => !y.has(v)), // only in A
    common: [...x].filter((v) => y.has(v))
  }
}

function typeDeltas(added: { type: string }[], removed: { type: string }[]): TypeDelta[] {
  const tally = new Map<string, TypeDelta>()
  const bump = (type: string, key: 'added' | 'removed'): void => {
    const row = tally.get(type) ?? { type, added: 0, removed: 0 }
    row[key] += 1
    tally.set(type, row)
  }
  for (const c of added) bump(c.type, 'added')
  for (const c of removed) bump(c.type, 'removed')
  // Most-changed types first; ties broken alphabetically for stable output.
  return [...tally.values()].sort(
    (a, b) => b.added + b.removed - (a.added + a.removed) || a.type.localeCompare(b.type)
  )
}

/** Structural drift from run `a` to run `b` (added = only in b, removed = only in a). */
export function diffModels(a: GraphModel, b: GraphModel): DiffResult {
  const aNodes = new Set(a.nodes.map((n) => n.id))
  const bNodes = new Set(b.nodes.map((n) => n.id))
  const aEdges = new Set(a.edges.map((e) => e.id))
  const bEdges = new Set(b.edges.map((e) => e.id))

  const nodeParts = partition(aNodes, bNodes)
  const edgeParts = partition(aEdges, bEdges)

  // Resolve display metadata against whichever side contains the item:
  // removed items live in A, added items live in B.
  const aNodeMap = new Map(a.nodes.map((n) => [n.id, n]))
  const bNodeMap = new Map(b.nodes.map((n) => [n.id, n]))
  const aEdgeMap = new Map(a.edges.map((e) => [e.id, e]))
  const bEdgeMap = new Map(b.edges.map((e) => [e.id, e]))
  const labelOf = (map: Map<string, KGNode>, id: string): string => map.get(id)?.label ?? id

  const nodeChange = (id: string, map: Map<string, KGNode>): DiffNode => {
    const n = map.get(id)
    return { id, type: n?.type ?? 'unknown', label: n?.label ?? id }
  }
  const edgeChange = (id: string, emap: Map<string, KGEdge>, nmap: Map<string, KGNode>): DiffEdge => {
    const e = emap.get(id)
    const src = e?.src ?? ''
    const dst = e?.dst ?? ''
    return { id, type: e?.type ?? 'unknown', src, dst, srcLabel: labelOf(nmap, src), dstLabel: labelOf(nmap, dst) }
  }

  const nodeChanges = {
    added: nodeParts.added.map((id) => nodeChange(id, bNodeMap)),
    removed: nodeParts.removed.map((id) => nodeChange(id, aNodeMap))
  }
  const edgeChanges = {
    added: edgeParts.added.map((id) => edgeChange(id, bEdgeMap, bNodeMap)),
    removed: edgeParts.removed.map((id) => edgeChange(id, aEdgeMap, aNodeMap))
  }

  const sum = (m: GraphModel) => ({
    runId: m.runId,
    runPath: m.runPath,
    nodeCount: m.nodes.length,
    edgeCount: m.edges.length,
    window: m.timeRange
  })

  return {
    a: sum(a),
    b: sum(b),
    nodes: nodeParts,
    edges: edgeParts,
    nodeChanges,
    edgeChanges,
    nodeTypeDeltas: typeDeltas(nodeChanges.added, nodeChanges.removed),
    edgeTypeDeltas: typeDeltas(edgeChanges.added, edgeChanges.removed)
  }
}
