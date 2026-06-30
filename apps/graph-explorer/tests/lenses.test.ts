import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LensStore } from '../src/main/lenses.js'
import { isLens } from '../src/shared/lenses.js'

// The LensStore is the persistence layer behind the "saved lenses" feature.
// It is deliberately decoupled from Electron (it takes a plain file path), so
// the full save/restart/delete lifecycle is exercised here under plain node —
// no app harness needed.

describe('LensStore', () => {
  let dir: string
  let file: string

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'lens-test-'))
    mkdirSync(join(dir, 'sub'))
    file = join(dir, 'sub', 'lenses.json')
  })

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true })
  })

  it('returns an empty list when nothing has been saved', () => {
    expect(new LensStore(file).list()).toEqual([])
    expect(existsSync(file)).toBe(false) // list() must not create the file
  })

  it('saves a lens and assigns an id + createdAt', () => {
    const store = new LensStore(file)
    const after = store.save({ name: 'reporting', engine: 'cypher', query: 'MATCH (n) RETURN n' })
    expect(after).toHaveLength(1)
    const lens = after[0]
    expect(isLens(lens)).toBe(true)
    expect(lens.id).toBeTruthy()
    expect(lens.name).toBe('reporting')
    expect(lens.engine).toBe('cypher')
    expect(typeof lens.createdAt).toBe('number')
  })

  it('persists across a "restart" (a fresh store over the same file)', () => {
    new LensStore(file).save({ name: 'mgmt', engine: 'sparql', query: 'SELECT ?x WHERE {}' })
    // A brand-new instance models the app being relaunched.
    const reopened = new LensStore(file).list()
    expect(reopened).toHaveLength(1)
    expect(reopened[0].name).toBe('mgmt')
    expect(reopened[0].engine).toBe('sparql')
  })

  it('overwrites an existing lens when saving with the same id', () => {
    const store = new LensStore(file)
    const [created] = store.save({ name: 'orig', engine: 'cypher', query: 'A' })
    const after = store.save({ id: created.id, name: 'renamed', engine: 'sparql', query: 'B' })
    expect(after).toHaveLength(1)
    expect(after[0].id).toBe(created.id)
    expect(after[0].name).toBe('renamed')
    expect(after[0].engine).toBe('sparql')
    expect(after[0].query).toBe('B')
  })

  it('deletes a lens by id and leaves the rest', () => {
    const store = new LensStore(file)
    const [a] = store.save({ name: 'a', engine: 'cypher', query: 'A' })
    store.save({ name: 'b', engine: 'cypher', query: 'B' })
    const after = store.delete(a.id)
    expect(after).toHaveLength(1)
    expect(after[0].name).toBe('b')
    expect(new LensStore(file).list()).toHaveLength(1) // delete persisted
  })

  it('delete is a no-op for an unknown id', () => {
    const store = new LensStore(file)
    store.save({ name: 'a', engine: 'cypher', query: 'A' })
    expect(store.delete('does-not-exist')).toHaveLength(1)
  })

  it('trims names/queries and rejects empty ones', () => {
    const store = new LensStore(file)
    const [lens] = store.save({ name: '  spacey  ', engine: 'cypher', query: '  Q  ' })
    expect(lens.name).toBe('spacey')
    expect(lens.query).toBe('Q')
    expect(() => store.save({ name: '   ', engine: 'cypher', query: 'Q' })).toThrow(/name/)
    expect(() => store.save({ name: 'ok', engine: 'cypher', query: '  ' })).toThrow(/query/)
  })

  it('degrades to an empty list on a corrupt file', () => {
    writeFileSync(file, '{ this is not json', 'utf8')
    expect(new LensStore(file).list()).toEqual([])
  })

  it('drops malformed entries but keeps well-formed ones', () => {
    const good = { id: 'x', name: 'good', engine: 'cypher', query: 'Q', createdAt: 1 }
    writeFileSync(file, JSON.stringify([good, { id: 'y' }, 42, null]), 'utf8')
    const list = new LensStore(file).list()
    expect(list).toHaveLength(1)
    expect(list[0].name).toBe('good')
  })

  it('keeps lenses ordered oldest-first', () => {
    const a = { id: 'a', name: 'a', engine: 'cypher', query: 'A', createdAt: 200 }
    const b = { id: 'b', name: 'b', engine: 'cypher', query: 'B', createdAt: 100 }
    writeFileSync(file, JSON.stringify([a, b]), 'utf8')
    expect(new LensStore(file).list().map((l) => l.id)).toEqual(['b', 'a'])
  })

  it('writes valid JSON to disk', () => {
    new LensStore(file).save({ name: 'a', engine: 'cypher', query: 'A' })
    const onDisk: unknown = JSON.parse(readFileSync(file, 'utf8'))
    expect(Array.isArray(onDisk)).toBe(true)
  })
})
