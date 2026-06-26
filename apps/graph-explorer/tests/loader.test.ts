import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { deriveLabel, loadRun } from '../src/sidecar/graph/loader.js'
import { goldenRunExists, loadGolden } from './helpers.js'
import type { GraphModel } from '../src/shared/model.js'

// The canonical node-type vocabulary the golden slice exercises. The loader
// sorts nodeTypes/edgeTypes, so this expected set is also sorted.
const EXPECTED_NODE_TYPES = [
  'Artifact',
  'CalendarEvent',
  'Company',
  'Department',
  'Goal',
  'Initiative',
  'Person',
  'Project',
  'Team'
].sort()

// deriveLabel() is pure and golden-independent — these always run.
describe('deriveLabel', () => {
  const base = { id: 'person:ben-cho', aliases: [] as string[], props: {} as Record<string, unknown> }

  it('prefers props.title over everything else', () => {
    expect(deriveLabel({ ...base, props: { title: 'Lead', name: 'Ben Cho', statement: 'x' } })).toBe('Lead')
  })

  it('falls back to props.name when title is absent', () => {
    expect(deriveLabel({ ...base, props: { name: 'Ben Cho', statement: 'x' } })).toBe('Ben Cho')
  })

  it('falls back to props.statement when title and name are absent', () => {
    expect(deriveLabel({ ...base, props: { statement: 'Ship v1' } })).toBe('Ship v1')
  })

  it('prefers a non-empty alias over the id', () => {
    expect(deriveLabel({ ...base, props: {}, aliases: ['Benjamin Cho'] })).toBe('Benjamin Cho')
  })

  it('falls back to the id when no label-bearing field exists', () => {
    expect(deriveLabel({ ...base, props: {}, aliases: [] })).toBe('person:ben-cho')
  })

  it('ignores blank/whitespace props and aliases', () => {
    expect(deriveLabel({ ...base, props: { title: '   ' }, aliases: ['  '] })).toBe('person:ben-cho')
  })
})

// loadRun() tolerates a malformed JSONL line — golden-independent, uses a temp run.
describe('loadRun malformed-line tolerance', () => {
  let runPath: string

  beforeAll(() => {
    runPath = mkdtempSync(join(tmpdir(), 'ge-loader-'))
    const kgDir = join(runPath, 'kg')
    mkdirSync(kgDir, { recursive: true })
    // Two valid node lines straddling one malformed line; loader must skip the
    // bad line rather than throw.
    const lines = [
      JSON.stringify({ id: 'a', type: 'Person', aliases: [], created_at: '', props: { name: 'A' } }),
      '{ this is not valid json ',
      JSON.stringify({ id: 'b', type: 'Person', aliases: [], created_at: '', props: { name: 'B' } })
    ]
    writeFileSync(join(kgDir, 'nodes.jsonl'), lines.join('\n') + '\n')
  })

  afterAll(() => {
    rmSync(runPath, { recursive: true, force: true })
  })

  it('skips the bad line and loads the surrounding valid nodes', () => {
    const model = loadRun(runPath)
    expect(model.nodes.map((n) => n.id)).toEqual(['a', 'b'])
    // Missing sibling files (edges, mentions, ...) degrade to empty, not fatal.
    expect(model.edges).toEqual([])
    expect(model.manifest).toBeNull()
  })
})

// Integration tests against the golden run. Skip cleanly when runs/ is absent.
describe.skipIf(!goldenRunExists())('loadGolden integration', () => {
  let model: GraphModel

  beforeAll(() => {
    model = loadGolden()
  })

  it('loads the expected node/edge counts', () => {
    expect(model.nodes).toHaveLength(56)
    expect(model.edges).toHaveLength(132)
  })

  it('derives the canonical node-type vocabulary', () => {
    expect(model.nodeTypes).toEqual(EXPECTED_NODE_TYPES)
  })

  it('derives a non-empty, sorted edge-type vocabulary', () => {
    expect(model.edgeTypes.length).toBeGreaterThan(0)
    expect([...model.edgeTypes].sort()).toEqual(model.edgeTypes)
    // A few structural edge types the golden org graph is guaranteed to have.
    expect(model.edgeTypes).toEqual(
      expect.arrayContaining(['reports_to', 'member_of', 'leads', 'owns'])
    )
  })

  it('derives labels from props, preferring them over the raw id', () => {
    // person:ben-cho carries props.title — deriveLabel prefers it over the id.
    const ben = model.nodes.find((n) => n.id === 'person:ben-cho')
    expect(ben).toBeDefined()
    expect(ben!.label).toBe('Platform / Infrastructure Lead')
    expect(ben!.label).not.toBe(ben!.id)
    // The company node has no title, so its label comes from props.name.
    const company = model.nodes.find((n) => n.id === 'company:golden-slice-co')
    expect(company).toBeDefined()
    expect(company!.label).toBe('Golden Slice Co')
  })

  it('exposes a non-null time range with start <= end', () => {
    expect(model.timeRange).not.toBeNull()
    const { start, end } = model.timeRange!
    expect(start).toBeTruthy()
    expect(end).toBeTruthy()
    expect(start <= end).toBe(true)
  })

  it('loads the auxiliary mention/provenance/alias/validation arrays', () => {
    expect(model.mentions.length).toBeGreaterThan(0)
    expect(model.provenance.length).toBeGreaterThan(0)
    expect(model.aliases.length).toBeGreaterThan(0)
    expect(model.validation.length).toBeGreaterThan(0)
  })
})
