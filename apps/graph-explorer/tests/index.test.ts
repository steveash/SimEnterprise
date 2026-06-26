import { describe, it, expect } from 'vitest'
import { GraphIndex } from '../src/sidecar/graph/index.js'
import type { GraphModel, KGNode, KGEdge, KGMention } from '../src/shared/model.js'
import { goldenRunExists, loadGolden } from './helpers.js'

// Unit tests for the in-memory GraphIndex (search / neighbors / shortestPath /
// provenance). Two complementary layers:
//
//   1. Synthetic models — tiny hand-built graphs that pin the *algorithm* exactly
//      (ranking tie-breaks, BFS depth/clamp, directed-vs-undirected, edge-type
//      filters, snippet fallbacks). These run on every checkout, no fixture
//      needed, so the core semantics are always covered.
//   2. Golden run — the same operations against the real gold KG, grounding the
//      assertions in production data (person:ben-cho & friends). Skip-guarded on
//      `goldenRunExists()` so a checkout without `runs/` still passes.

// --- synthetic-model builders -------------------------------------------------

function node(id: string, type: string, label: string, extra: Partial<KGNode> = {}): KGNode {
  return { id, type, label, aliases: [], created_at: '2026-01-01T00:00:00', props: {}, ...extra }
}

function edge(id: string, type: string, src: string, dst: string): KGEdge {
  return { id, type, src, dst, created_at: '2026-01-01T00:00:00', props: {} }
}

/** Build a minimal-but-complete GraphModel from nodes/edges/mentions. */
function model(parts: Partial<GraphModel> & { nodes: KGNode[] }): GraphModel {
  return {
    runId: 'synthetic',
    runPath: '/nonexistent-run-root',
    nodes: parts.nodes,
    edges: parts.edges ?? [],
    mentions: parts.mentions ?? [],
    events: [],
    provenance: parts.provenance ?? [],
    aliases: [],
    validation: [],
    manifest: null,
    nodeTypes: [...new Set(parts.nodes.map((n) => n.type))],
    edgeTypes: [...new Set((parts.edges ?? []).map((e) => e.type))],
    timeRange: null
  }
}

/** a→b→c (type 'rel') plus a→d (type 'other'), with an isolated 'island'. */
function diamondIndex(): GraphIndex {
  return new GraphIndex(
    model({
      nodes: [
        node('a', 'N', 'A'),
        node('b', 'N', 'B'),
        node('c', 'N', 'C'),
        node('d', 'N', 'D'),
        node('island', 'N', 'Island')
      ],
      edges: [edge('e_ab', 'rel', 'a', 'b'), edge('e_bc', 'rel', 'b', 'c'), edge('e_ad', 'other', 'a', 'd')]
    })
  )
}

// =============================================================================
// search()
// =============================================================================

describe('GraphIndex.search (synthetic)', () => {
  const ix = new GraphIndex(
    model({
      nodes: [
        node('person:alice', 'Person', 'Alice', { aliases: ['quux'], props: { motto: 'hello world' } }),
        node('person:alice-smith', 'Person', 'Alice Smith'),
        node('team:zeta', 'Team', 'Zeta'),
        node('doc:42', 'Artifact', 'Nothing Here')
      ]
    })
  )

  it('ranks an exact label match above a substring match', () => {
    const hits = ix.search('Alice')
    expect(hits[0].id).toBe('person:alice')
    expect(hits[0].score).toBe(100)
    expect(hits[0].matched).toBe('label')
    // 'Alice Smith' only contains the query — strictly lower score, still a label hit.
    const sub = hits.find((h) => h.id === 'person:alice-smith')!
    expect(sub.matched).toBe('label')
    expect(sub.score).toBeLessThan(100)
    expect(sub.score).toBeGreaterThan(0)
  })

  it('returns [] for an empty or whitespace-only query', () => {
    expect(ix.search('')).toEqual([])
    expect(ix.search('   ')).toEqual([])
  })

  it('is case-insensitive', () => {
    expect(ix.search('ALICE')[0].id).toBe('person:alice')
  })

  it('restricts results with a type filter', () => {
    const persons = ix.search('Alice', { types: ['Person'] })
    expect(persons.length).toBe(2)
    expect(persons.every((h) => h.type === 'Person')).toBe(true)
    // No Team node matches 'Alice', so the filter empties the result.
    expect(ix.search('Alice', { types: ['Team'] })).toEqual([])
  })

  it('falls back through id / alias / props in priority order', () => {
    const byId = ix.search('42')[0]
    expect(byId.id).toBe('doc:42')
    expect(byId.matched).toBe('id')
    expect(byId.score).toBe(50)

    const byAlias = ix.search('quux')[0]
    expect(byAlias.id).toBe('person:alice')
    expect(byAlias.matched).toBe('alias')
    expect(byAlias.score).toBe(45)

    const byProps = ix.search('hello world')[0]
    expect(byProps.id).toBe('person:alice')
    expect(byProps.matched).toBe('props.motto')
    expect(byProps.score).toBe(30)
  })

  it('honors the limit option', () => {
    expect(ix.search('Alice', { limit: 1 })).toHaveLength(1)
  })
})

// =============================================================================
// neighbors()
// =============================================================================

describe('GraphIndex.neighbors (synthetic)', () => {
  it('grows the subgraph as depth increases and always includes the seed', () => {
    const ix = diamondIndex()
    const d1 = ix.neighbors('a', { depth: 1 })
    const d2 = ix.neighbors('a', { depth: 2 })
    expect(d1.nodeIds).toContain('a')
    expect(new Set(d1.nodeIds)).toEqual(new Set(['a', 'b', 'd']))
    // depth 2 reaches c through b — strictly larger.
    expect(d2.nodeIds.length).toBeGreaterThan(d1.nodeIds.length)
    expect(d2.nodeIds).toContain('c')
  })

  it('follows only the requested direction', () => {
    const ix = diamondIndex()
    const out = ix.neighbors('a', { depth: 1, direction: 'out' })
    const inc = ix.neighbors('a', { depth: 1, direction: 'in' })
    expect(new Set(out.nodeIds)).toEqual(new Set(['a', 'b', 'd']))
    // Nothing points *into* a, so the in-set is just the seed.
    expect(inc.nodeIds).toEqual(['a'])
    expect(out.nodeIds.length).not.toBe(inc.nodeIds.length)
  })

  it('limits traversal to the requested edge types', () => {
    const ix = diamondIndex()
    const relOnly = ix.neighbors('a', { depth: 1, edgeTypes: ['rel'] })
    // e_ad is type 'other' — excluded, so d is unreachable.
    expect(new Set(relOnly.nodeIds)).toEqual(new Set(['a', 'b']))
    expect(relOnly.edgeIds).toEqual(['e_ab'])
  })

  it('clamps depth into [1, 6]', () => {
    // A straight chain c0 -> c1 -> ... -> c8.
    const chain = new GraphIndex(
      model({
        nodes: Array.from({ length: 9 }, (_, i) => node(`c${i}`, 'N', `C${i}`)),
        edges: Array.from({ length: 8 }, (_, i) => edge(`l${i}`, 'link', `c${i}`, `c${i + 1}`))
      })
    )
    // depth 0 clamps up to 1: seed + one hop.
    expect(new Set(chain.neighbors('c0', { depth: 0 }).nodeIds)).toEqual(new Set(['c0', 'c1']))
    // depth 99 clamps down to 6: reaches c6 (7 nodes), not the whole chain.
    const deep = chain.neighbors('c0', { depth: 99 })
    expect(deep.nodeIds).toHaveLength(7)
    expect(deep.nodeIds).not.toContain('c7')
  })
})

// =============================================================================
// shortestPath()
// =============================================================================

describe('GraphIndex.shortestPath (synthetic)', () => {
  it('returns a contiguous hit with hops > 0', () => {
    const ix = diamondIndex()
    const p = ix.shortestPath('a', 'c')
    expect(p).not.toBeNull()
    expect(p!.nodeIds[0]).toBe('a')
    expect(p!.nodeIds[p!.nodeIds.length - 1]).toBe('c')
    const hops = p!.edgeIds.length
    expect(hops).toBeGreaterThan(0)
    // Reconstructed node/edge ids line up: edge i connects node i to node i+1.
    expect(p!.nodeIds).toHaveLength(hops + 1)
    const byId = new Map(ix.model.edges.map((e) => [e.id, e]))
    p!.edgeIds.forEach((eid, i) => {
      const e = byId.get(eid)!
      const pair = new Set([e.src, e.dst])
      expect(pair).toEqual(new Set([p!.nodeIds[i], p!.nodeIds[i + 1]]))
    })
  })

  it('returns 0 hops for from === to', () => {
    const ix = diamondIndex()
    const p = ix.shortestPath('a', 'a')
    expect(p).toEqual({ nodeIds: ['a'], edgeIds: [] })
  })

  it('returns null for a disconnected pair', () => {
    const ix = diamondIndex()
    expect(ix.shortestPath('a', 'island')).toBeNull()
  })

  it('treats edges as undirected by default but respects directed', () => {
    const ix = diamondIndex()
    // c -> a does not exist forward; only a -> b -> c does.
    expect(ix.shortestPath('c', 'a')).not.toBeNull()
    expect(ix.shortestPath('c', 'a', { directed: true })).toBeNull()
  })

  it('filters traversal by edge type', () => {
    const ix = diamondIndex()
    // a -> d is via 'other'; excluding it leaves d unreachable.
    expect(ix.shortestPath('a', 'd')).not.toBeNull()
    expect(ix.shortestPath('a', 'd', { edgeTypes: ['rel'] })).toBeNull()
  })
})

// =============================================================================
// provenance()
// =============================================================================

describe('GraphIndex.provenance (synthetic)', () => {
  it('merges + dedupes artifacts from mentions and provenance, with null snippet when the file is missing', () => {
    const mention: KGMention = {
      artifact_path: 'nope.md',
      entity_id: 'n:x',
      surface_form: 'X',
      locator: { medium: 'markdown', offset: 0, length: 1, line: 1 }
    }
    const ix = new GraphIndex(
      model({
        nodes: [node('n:x', 'N', 'Node X')],
        mentions: [mention],
        provenance: [{ target_id: 'n:x', artifacts: [{ path: 'extra.md' }, { path: 'nope.md' }] }]
      })
    )
    const prov = ix.provenance('n:x')
    expect(prov.label).toBe('Node X')
    expect(prov.mentions).toHaveLength(1)
    // runPath is bogus, so the artifact can't be read.
    expect(prov.mentions[0].snippet).toBeNull()
    // 'nope.md' appears in both sources but only once; 'extra.md' from provenance.
    expect(new Set(prov.artifacts)).toEqual(new Set(['nope.md', 'extra.md']))
  })

  it('returns an empty, id-labeled result for an unknown node', () => {
    const ix = new GraphIndex(model({ nodes: [] }))
    const prov = ix.provenance('person:ghost')
    expect(prov).toEqual({ nodeId: 'person:ghost', label: 'person:ghost', mentions: [], artifacts: [] })
  })
})

// =============================================================================
// Golden run — real gold KG data (skips cleanly without runs/)
// =============================================================================

describe.skipIf(!goldenRunExists())('GraphIndex against the golden run', () => {
  const ix = new GraphIndex(loadGolden())
  const BEN = 'person:ben-cho'

  it('finds person:ben-cho for the query "Ben"', () => {
    const hits = ix.search('Ben')
    expect(hits.length).toBeGreaterThan(0)
    expect(hits[0].id).toBe(BEN)
    expect(ix.search('')).toEqual([])
  })

  it('restricts search to a node type', () => {
    const all = ix.search('engineering')
    const persons = ix.search('engineering', { types: ['Person'] })
    expect(persons.length).toBeGreaterThan(0)
    expect(persons.every((h) => h.type === 'Person')).toBe(true)
    // Unfiltered surfaces more than one node type.
    expect(new Set(all.map((h) => h.type)).size).toBeGreaterThan(1)
  })

  it('neighbors grow with depth and differ by direction / edge type', () => {
    const d1 = ix.neighbors(BEN, { depth: 1 })
    const d2 = ix.neighbors(BEN, { depth: 2 })
    expect(d1.nodeIds).toContain(BEN)
    expect(d2.nodeIds.length).toBeGreaterThan(d1.nodeIds.length)

    const out = ix.neighbors(BEN, { depth: 1, direction: 'out' })
    const inc = ix.neighbors(BEN, { depth: 1, direction: 'in' })
    expect(out.nodeIds.length).not.toBe(inc.nodeIds.length)

    const memberOf = ix.neighbors(BEN, { depth: 1, edgeTypes: ['member_of'] })
    expect(memberOf.edgeIds.length).toBeGreaterThan(0)
    expect(memberOf.nodeIds.length).toBeLessThan(d1.nodeIds.length)
    const edgeType = new Map(ix.model.edges.map((e) => [e.id, e.type]))
    expect(memberOf.edgeIds.every((id) => edgeType.get(id) === 'member_of')).toBe(true)
  })

  it('shortestPath: hit (contiguous), self (0 hops), miss (null)', () => {
    const p = ix.shortestPath(BEN, 'person:cleo-costa')
    expect(p).not.toBeNull()
    expect(p!.nodeIds[0]).toBe(BEN)
    expect(p!.nodeIds[p!.nodeIds.length - 1]).toBe('person:cleo-costa')
    expect(p!.edgeIds.length).toBeGreaterThan(0)
    expect(p!.nodeIds).toHaveLength(p!.edgeIds.length + 1)

    expect(ix.shortestPath(BEN, BEN)).toEqual({ nodeIds: [BEN], edgeIds: [] })
    expect(ix.shortestPath(BEN, 'person:nobody-does-not-exist')).toBeNull()
  })

  it('provenance: mentions with snippets read from artifacts, plus artifact list', () => {
    const prov = ix.provenance(BEN)
    expect(prov.mentions.length).toBeGreaterThan(0)
    expect(prov.artifacts.length).toBeGreaterThan(0)
    // At least one snippet is read back from a real artifact (markdown lines).
    const md = prov.mentions.find((m) => m.artifact_path.endsWith('.md'))!
    expect(md.snippet).not.toBeNull()
    expect(md.snippet!.toLowerCase()).toContain('ben')
    // Every mentioned artifact is present in the merged artifacts list.
    expect(new Set(prov.artifacts)).toEqual(new Set([...new Set(prov.mentions.map((m) => m.artifact_path))]))
  })
})
