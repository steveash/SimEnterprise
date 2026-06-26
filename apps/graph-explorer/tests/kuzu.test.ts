import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { goldenRunExists, loadGolden } from './helpers.js'
import { KuzuEngine } from '../src/sidecar/graph/kuzu.js'
import type { GraphModel } from '../src/shared/model.js'

// Exercises the embedded Cypher engine over the golden model: typed schema build,
// reserved-word column handling (the regression that bit during the build),
// direct + variable-length traversal, and the agent-facing schema description.
//
// Skips cleanly (exit 0) on a checkout without `runs/`, mirroring smoke.test.ts.
describe.skipIf(!goldenRunExists())('KuzuEngine over the golden model', () => {
  let model: GraphModel
  let engine: KuzuEngine

  beforeAll(async () => {
    model = loadGolden()
    engine = await KuzuEngine.build(model)
  })

  afterAll(() => {
    engine?.close()
  })

  it('builds one node table per node type', () => {
    const labels = engine.schema.nodeTables.map((t) => t.label).sort()
    expect(labels).toEqual([...model.nodeTypes].sort())
    // Every type in the model is represented exactly once.
    expect(labels.length).toBe(new Set(labels).size)
    expect(labels.length).toBe(model.nodeTypes.length)
  })

  it('rel tables carry every observed FROM/TO pair (member_of: Person->Team and Person->Project)', () => {
    const memberOf = engine.schema.relTables.find((t) => t.label === 'member_of')
    expect(memberOf).toBeDefined()
    const pairs = memberOf!.pairs.map(([s, d]) => `${s}->${d}`)
    expect(pairs).toContain('Person->Team')
    expect(pairs).toContain('Person->Project')
  })

  it('rewrites reserved-word props to safe columns (CalendarEvent start/end)', () => {
    const cal = engine.schema.nodeTables.find((t) => t.label === 'CalendarEvent')
    expect(cal).toBeDefined()
    // The raw prop keys 'start'/'end' are Cypher reserved words and must not
    // appear as bare columns — they are sanitized (p_start / p_end or similar).
    expect(cal!.columns).not.toContain('start')
    expect(cal!.columns).not.toContain('end')
    const startCol = cal!.columns.find((c) => c !== 'start' && /start$/i.test(c))
    const endCol = cal!.columns.find((c) => c !== 'end' && /end$/i.test(c))
    expect(startCol).toBeDefined()
    expect(endCol).toBeDefined()
  })

  it('queries the reserved-word columns without throwing (the regression)', async () => {
    const cal = engine.schema.nodeTables.find((t) => t.label === 'CalendarEvent')!
    const startCol = cal.columns.find((c) => c !== 'start' && /start$/i.test(c))!
    const endCol = cal.columns.find((c) => c !== 'end' && /end$/i.test(c))!
    const res = await engine.query(
      `MATCH (c:CalendarEvent) RETURN c.${startCol}, c.${endCol} LIMIT 5`
    )
    expect(res.rows.length).toBeGreaterThan(0)
    expect(res.columns.length).toBe(2)
  })

  it('answers a direct reports_to traversal (8 rows)', async () => {
    const res = await engine.query(
      'MATCH (p:Person)-[:reports_to]->(m:Person) RETURN p.id, m.id'
    )
    expect(res.rows.length).toBe(8)
  })

  it('answers a variable-length reports_to traversal (> direct)', async () => {
    const res = await engine.query(
      'MATCH (p:Person)-[:reports_to*1..]->(m:Person) RETURN p.id, m.id'
    )
    // Transitive closure over the management chain yields strictly more than the
    // 8 direct edges.
    expect(res.rows.length).toBeGreaterThan(8)
  })

  it('describeSchema() returns non-empty text naming node and rel tables', () => {
    const text = engine.describeSchema()
    expect(text.length).toBeGreaterThan(0)
    expect(text).toContain('NODE TABLES')
    expect(text).toContain('REL TABLES')
    expect(text).toContain('Person')
    expect(text).toContain('reports_to')
  })
})
