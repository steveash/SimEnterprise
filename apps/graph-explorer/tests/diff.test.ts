import { describe, it, expect } from 'vitest'
import { diffModels } from '../src/sidecar/graph/diff.js'
import type { GraphModel, KGNode, KGEdge } from '../src/shared/model.js'

// Unit tests for the pure run-to-run structural diff (diffModels). Hand-built
// models pin the typed-breakdown + change-resolution semantics that the polished
// DiffPanel renders (per-type counts, edge type labels, endpoint labels, the
// add=only-in-B / remove=only-in-A orientation). No fixture needed.

function node(id: string, type: string, label: string): KGNode {
  return { id, type, label, aliases: [], created_at: '2026-01-01T00:00:00', props: {} }
}
function edge(id: string, type: string, src: string, dst: string): KGEdge {
  return { id, type, src, dst, created_at: '2026-01-01T00:00:00', props: {} }
}
function model(runId: string, nodes: KGNode[], edges: KGEdge[]): GraphModel {
  return {
    runId,
    runPath: `/runs/${runId}`,
    nodes,
    edges,
    mentions: [],
    events: [],
    provenance: [],
    aliases: [],
    validation: [],
    manifest: null,
    nodeTypes: [...new Set(nodes.map((n) => n.type))],
    edgeTypes: [...new Set(edges.map((e) => e.type))],
    timeRange: null
  }
}

describe('diffModels', () => {
  it('self-diff shows zero drift and empty typed breakdowns', () => {
    const m = model('a', [node('p1', 'Person', 'Ada'), node('t1', 'Team', 'Core')], [edge('e1', 'member_of', 'p1', 't1')])
    const d = diffModels(m, m)
    expect(d.nodes.added).toEqual([])
    expect(d.nodes.removed).toEqual([])
    expect(d.edges.added).toEqual([])
    expect(d.edges.removed).toEqual([])
    expect(d.nodes.common.length).toBe(d.a.nodeCount)
    expect(d.edges.common.length).toBe(d.a.edgeCount)
    expect(d.nodeChanges.added).toEqual([])
    expect(d.nodeChanges.removed).toEqual([])
    expect(d.nodeTypeDeltas).toEqual([])
    expect(d.edgeTypeDeltas).toEqual([])
  })

  it('resolves typed node/edge changes with the right add/remove orientation', () => {
    const a = model(
      'A',
      [node('p1', 'Person', 'Ada'), node('p2', 'Person', 'Bo'), node('t1', 'Team', 'Core')],
      [edge('e1', 'member_of', 'p1', 't1'), edge('e2', 'reports_to', 'p2', 'p1')]
    )
    const b = model(
      'B',
      // p2 dropped, t1 kept, p1 kept; new Goal g1 added.
      [node('p1', 'Person', 'Ada'), node('t1', 'Team', 'Core'), node('g1', 'Goal', 'Ship it')],
      // e2 dropped (was on p2); e1 kept; new edge e3 of a new type added.
      [edge('e1', 'member_of', 'p1', 't1'), edge('e3', 'advances_goal', 'p1', 'g1')]
    )
    const d = diffModels(a, b)

    // added = only in B, removed = only in A.
    expect(d.nodes.added).toEqual(['g1'])
    expect(d.nodes.removed).toEqual(['p2'])
    expect(d.edges.added).toEqual(['e3'])
    expect(d.edges.removed).toEqual(['e2'])

    // Added node resolved against B; removed node resolved against A.
    expect(d.nodeChanges.added).toEqual([{ id: 'g1', type: 'Goal', label: 'Ship it' }])
    expect(d.nodeChanges.removed).toEqual([{ id: 'p2', type: 'Person', label: 'Bo' }])

    // Edge changes carry type + endpoint labels resolved against the owning side.
    expect(d.edgeChanges.added).toEqual([
      { id: 'e3', type: 'advances_goal', src: 'p1', dst: 'g1', srcLabel: 'Ada', dstLabel: 'Ship it' }
    ])
    expect(d.edgeChanges.removed).toEqual([
      { id: 'e2', type: 'reports_to', src: 'p2', dst: 'p1', srcLabel: 'Bo', dstLabel: 'Ada' }
    ])

    // Per-type breakdowns.
    expect(d.nodeTypeDeltas).toEqual([
      { type: 'Goal', added: 1, removed: 0 },
      { type: 'Person', added: 0, removed: 1 }
    ])
    expect(d.edgeTypeDeltas).toEqual([
      { type: 'advances_goal', added: 1, removed: 0 },
      { type: 'reports_to', added: 0, removed: 1 }
    ])
  })

  it('orders type breakdowns by total magnitude then alphabetically', () => {
    const a = model('A', [node('t1', 'Team', 'A')], [])
    const b = model(
      'B',
      [
        node('p1', 'Person', 'A'),
        node('p2', 'Person', 'B'),
        node('z1', 'Zeta', 'Z'),
        node('m1', 'Mu', 'M'),
        node('t1', 'Team', 'A')
      ],
      []
    )
    const d = diffModels(a, b)
    // Person (2) leads; Mu and Zeta tie at 1 → alphabetical.
    expect(d.nodeTypeDeltas).toEqual([
      { type: 'Person', added: 2, removed: 0 },
      { type: 'Mu', added: 1, removed: 0 },
      { type: 'Zeta', added: 1, removed: 0 }
    ])
  })
})
