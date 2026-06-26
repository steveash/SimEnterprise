// Unit tests for the embedded SPARQL reasoning layer (src/sidecar/graph/rdf.ts).
//
// Two complementary fixtures:
//
//   1. A small SYNTHETIC GraphModel built inline. It exercises every inference
//      rule with hand-picked structure (including goal propagation up the
//      subgoal tree, which the golden slice happens not to contain), the
//      SELECT/ASK/CONSTRUCT result shapes, and the prefix-injection regression.
//      These run on every checkout — they need no `runs/` directory.
//
//   2. The GOLDEN model (`loadGolden()`), skip-guarded on `goldenRunExists()`
//      exactly like smoke.test.ts. It locks the exact materialized output of the
//      reasoner against the deterministic gold KG (inferredCount === 85, the
//      reports_to transitive closure, in_department -> Engineering, the manages
//      inverse). `runs/` is gitignored, so these skip cleanly on a fresh
//      checkout and assert hard numbers locally / wherever the golden run exists.

import { describe, it, expect } from 'vitest'
import { OxigraphEngine } from '../src/sidecar/graph/rdf.js'
import { goldenRunExists, loadGolden } from './helpers.js'
import type { GraphModel, KGNode, KGEdge } from '../src/shared/model.js'

// ---------------------------------------------------------------------------
// Synthetic fixture
// ---------------------------------------------------------------------------

function node(id: string, type: string, props: Record<string, unknown> = {}): KGNode {
  return { id, type, label: id, aliases: [], created_at: '2026-01-01T00:00:00Z', props }
}

function edge(type: string, src: string, dst: string): KGEdge {
  return { id: `${src}|${type}|${dst}`, type, src, dst, created_at: '2026-01-01T00:00:00Z', props: {} }
}

/**
 * A tiny org whose structure triggers every rule in INFERENCE_RULES:
 *   carol -> bob -> alice   (reports_to chain; carol->alice is a 2-hop entailment)
 *   alice leads engineering (in_department via leadership)
 *   carol member_of eng-team, eng-team part_of engineering (in_department via membership)
 *   init-x advances_goal g-child, g-child subgoal_of g-parent
 *       (advances_goal_effective must propagate up to g-parent)
 *   bob collaborates_with carol (symmetric edge)
 */
function syntheticModel(): GraphModel {
  const nodes = [
    node('p-alice', 'Person', { seniority: 'lead' }),
    node('p-bob', 'Person'),
    node('p-carol', 'Person'),
    node('t-eng', 'Team'),
    node('d-eng', 'Department'),
    node('g-parent', 'Goal'),
    node('g-child', 'Goal'),
    node('init-x', 'Initiative')
  ]
  const edges = [
    edge('reports_to', 'p-bob', 'p-alice'),
    edge('reports_to', 'p-carol', 'p-bob'),
    edge('leads', 'p-alice', 'd-eng'),
    edge('member_of', 'p-carol', 't-eng'),
    edge('part_of', 't-eng', 'd-eng'),
    edge('advances_goal', 'init-x', 'g-child'),
    edge('subgoal_of', 'g-child', 'g-parent'),
    edge('collaborates_with', 'p-bob', 'p-carol')
  ]
  return {
    runId: 'synthetic',
    runPath: '',
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

describe('OxigraphEngine — synthetic model (rule coverage)', () => {
  const eng = OxigraphEngine.build(syntheticModel())

  it('materialization runs (inferredCount > 0)', () => {
    expect(eng.inferredCount).toBeGreaterThan(0)
  })

  it('build() terminates — fixpoint guard does not hang', () => {
    // Reaching this assertion at all proves materialize() returned. Rebuild a
    // fresh engine to confirm it is not a one-time fluke of the shared instance.
    const fresh = OxigraphEngine.build(syntheticModel())
    expect(fresh.inferredCount).toBe(eng.inferredCount)
  })

  it('manages is the inverse of reports_to', () => {
    // bob reports_to alice  =>  alice manages bob
    expect(eng.query(`ASK { ent:p-alice der:manages ent:p-bob }`).boolean).toBe(true)
    expect(eng.query(`ASK { ent:p-bob der:manages ent:p-alice }`).boolean).toBe(false)

    const managed = eng.query(`SELECT ?p WHERE { ent:p-bob der:manages ?p }`)
    expect(managed.rows.map((r) => r.p).sort()).toEqual(['p-carol'])
  })

  it('reports_to_chain is the transitive closure of reports_to', () => {
    const chain = eng.query(`SELECT ?p ?m WHERE { ?p der:reports_to_chain ?m }`)
    const pairs = new Set(chain.rows.map((r) => `${r.p}->${r.m}`))
    // direct edges present
    expect(pairs.has('p-bob->p-alice')).toBe(true)
    expect(pairs.has('p-carol->p-bob')).toBe(true)
    // 2-hop entailment that is NOT a direct reports_to edge
    expect(pairs.has('p-carol->p-alice')).toBe(true)
    const direct = eng.query(`SELECT ?p ?m WHERE { ?p rel:reports_to ?m }`)
    const directPairs = new Set(direct.rows.map((r) => `${r.p}->${r.m}`))
    expect(directPairs.has('p-carol->p-alice')).toBe(false)
  })

  it('in_department joins people to a Department via membership and leadership', () => {
    const all = eng.query(`SELECT ?p ?d WHERE { ?p der:in_department ?d }`)
    expect(all.rows.length).toBeGreaterThan(0)
    // carol reaches engineering via team membership (member_of -> part_of)
    expect(eng.query(`ASK { ent:p-carol der:in_department ent:d-eng }`).boolean).toBe(true)
    // alice reaches engineering via leadership (leads + Department type)
    expect(eng.query(`ASK { ent:p-alice der:in_department ent:d-eng }`).boolean).toBe(true)
  })

  it('advances_goal_effective propagates up the goal tree via subgoal_of_chain', () => {
    // Base assertion: init-x advances g-child directly.
    expect(eng.query(`ASK { ent:init-x rel:advances_goal ent:g-child }`).boolean).toBe(true)
    // It does NOT directly advance the parent...
    expect(eng.query(`ASK { ent:init-x rel:advances_goal ent:g-parent }`).boolean).toBe(false)
    // ...but the inferred effective relation reaches the parent (g-child subgoal_of g-parent).
    expect(eng.query(`ASK { ent:init-x der:advances_goal_effective ent:g-child }`).boolean).toBe(true)
    expect(eng.query(`ASK { ent:init-x der:advances_goal_effective ent:g-parent }`).boolean).toBe(true)
  })

  it('collaborates_with is symmetric', () => {
    expect(eng.query(`ASK { ent:p-carol rel:collaborates_with ent:p-bob }`).boolean).toBe(true)
  })
})

describe('OxigraphEngine — query() result shapes', () => {
  const eng = OxigraphEngine.build(syntheticModel())

  it('SELECT returns {kind:"select", columns, rows}', () => {
    const res = eng.query(`SELECT ?p ?m WHERE { ?p der:manages ?m }`)
    expect(res.kind).toBe('select')
    expect(res.columns).toEqual(['p', 'm'])
    expect(res.rows.length).toBeGreaterThan(0)
    for (const row of res.rows) {
      expect(Object.keys(row).sort()).toEqual(['m', 'p'])
    }
  })

  it('ASK returns a boolean', () => {
    const yes = eng.query(`ASK { ?p rel:reports_to ?m }`)
    expect(yes.kind).toBe('ask')
    expect(yes.boolean).toBe(true)

    const no = eng.query(`ASK { ent:p-alice rel:reports_to ent:p-carol }`)
    expect(no.kind).toBe('ask')
    expect(no.boolean).toBe(false)
  })

  it('CONSTRUCT returns subject/predicate/object rows', () => {
    const res = eng.query(`CONSTRUCT { ?p der:manages ?m } WHERE { ?p der:manages ?m }`)
    expect(res.kind).toBe('construct')
    expect(res.columns).toEqual(['subject', 'predicate', 'object'])
    expect(res.rows.length).toBeGreaterThan(0)
    for (const row of res.rows) {
      expect(Object.keys(row).sort()).toEqual(['object', 'predicate', 'subject'])
      // predicate is shortened back to a prefixed name for display
      expect(row.predicate).toBe('der:manages')
    }
  })
})

describe('OxigraphEngine — prefix injection (live-agent regression)', () => {
  const eng = OxigraphEngine.build(syntheticModel())

  it('(a) a query with NO declared prefixes still runs (all auto-injected)', () => {
    const res = eng.query(`SELECT ?s WHERE { ?s rdf:type cls:Person }`)
    expect(res.kind).toBe('select')
    expect(res.rows.length).toBeGreaterThan(0)
  })

  it('(b) a query declaring only one prefix but using others undeclared still runs', () => {
    // Declares es: (unused here) yet references der:/rdf:/cls: with no PREFIX line —
    // the exact shape of the live-agent bug. Only the missing prefixes are injected.
    const res = eng.query(
      `PREFIX es: <http://enterprise-sim/>
SELECT ?p ?m WHERE { ?p der:manages ?m . ?p rdf:type cls:Person }`
    )
    expect(res.kind).toBe('select')
    expect(res.rows.length).toBeGreaterThan(0)
  })

  it('(c) redeclaring a standard prefix does NOT cause a duplicate-declaration error', () => {
    const res = eng.query(
      `PREFIX der: <http://enterprise-sim/derived/>
SELECT ?p ?m WHERE { ?p der:manages ?m }`
    )
    expect(res.kind).toBe('select')
    expect(res.rows.length).toBeGreaterThan(0)
  })

  it('a redeclared prefix with a DIFFERENT uri is respected (not overridden by injection)', () => {
    // Proves injection is additive-only: the user's der: binding wins, so this
    // points der: at the entity namespace and resolves entities, not derived edges.
    const res = eng.query(
      `PREFIX der: <http://enterprise-sim/entity/>
ASK { der:p-bob rel:reports_to der:p-alice }`
    )
    expect(res.kind).toBe('ask')
    expect(res.boolean).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Golden model — exact materialized output of the deterministic gold KG.
// Skips cleanly (exit 0) on a checkout without `runs/`.
// ---------------------------------------------------------------------------

describe.skipIf(!goldenRunExists())('OxigraphEngine — golden model', () => {
  const eng = OxigraphEngine.build(loadGolden())

  it('materializes exactly 85 inferred triples', () => {
    expect(eng.inferredCount).toBe(85)
  })

  it('reports_to_chain is the transitive closure of reports_to', () => {
    const direct = eng.query(`SELECT ?p ?m WHERE { ?p rel:reports_to ?m }`)
    const chain = eng.query(`SELECT ?p ?m WHERE { ?p der:reports_to_chain ?m }`)
    const directPairs = new Set(direct.rows.map((r) => `${r.p}->${r.m}`))
    const chainPairs = new Set(chain.rows.map((r) => `${r.p}->${r.m}`))

    // every direct edge is present in the closure
    for (const p of directPairs) expect(chainPairs.has(p)).toBe(true)
    // the closure is strictly larger (it added transitive entailments)
    expect(chainPairs.size).toBeGreaterThan(directPairs.size)
    // a specific 2-hop entailment that is NOT a direct reports_to edge:
    //   quinn-greco -> ben-cho -> yuki-quintero
    expect(directPairs.has('person:quinn-greco->person:ben-cho')).toBe(true)
    expect(directPairs.has('person:ben-cho->person:yuki-quintero')).toBe(true)
    expect(chainPairs.has('person:quinn-greco->person:yuki-quintero')).toBe(true)
    expect(directPairs.has('person:quinn-greco->person:yuki-quintero')).toBe(false)
  })

  it('in_department maps a sample person to Engineering', () => {
    const rows = eng.query(`SELECT ?p ?d WHERE { ?p der:in_department ?d }`)
    expect(rows.rows.length).toBeGreaterThan(0)
    // ben-cho lands in the engineering department
    const benDept = rows.rows.filter((r) => r.p === 'person:ben-cho').map((r) => r.d)
    expect(benDept).toContain('dept:engineering')
    // and that department is labelled "Engineering"
    const label = eng.query(
      `SELECT ?l WHERE { ent:dept%3Aengineering rdfs:label ?l }`
    )
    expect(label.rows.map((r) => r.l)).toContain('Engineering')
  })

  it('manages is the inverse of reports_to for a known pair', () => {
    // ben-cho reports_to yuki-quintero  =>  yuki-quintero manages ben-cho
    const reportsPair = eng.query(
      `ASK { ent:person%3Aben-cho rel:reports_to ent:person%3Ayuki-quintero }`
    )
    expect(reportsPair.boolean).toBe(true)
    const managesPair = eng.query(
      `ASK { ent:person%3Ayuki-quintero der:manages ent:person%3Aben-cho }`
    )
    expect(managesPair.boolean).toBe(true)
  })

  it('advances_goal_effective covers every asserted advances_goal edge', () => {
    const base = eng.query(`SELECT ?x ?g WHERE { ?x rel:advances_goal ?g }`)
    const eff = eng.query(`SELECT ?x ?g WHERE { ?x der:advances_goal_effective ?g }`)
    expect(base.rows.length).toBeGreaterThan(0)
    const effPairs = new Set(eff.rows.map((r) => `${r.x}->${r.g}`))
    for (const r of base.rows) expect(effPairs.has(`${r.x}->${r.g}`)).toBe(true)
  })
})
