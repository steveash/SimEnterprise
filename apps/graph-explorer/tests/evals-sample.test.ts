// Parity: src/sidecar/evals/sample.ts against tests/fixtures/evals_sample_parity.json,
// generated from enterprise_sim.evals.sample (EXPLORER_EVALS.md §3/§6): the
// mulberry32 PRNG and the seeded Fisher-Yates sampler, stratified or not.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Mulberry32, sample, sampleIds, seededShuffle } from '../src/sidecar/evals/sample.js'

const FIXTURE = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'tests', 'fixtures', 'evals_sample_parity.json')

interface Case {
  name: string
  ids: string[]
  fraction: number
  seed: number
  stratify: boolean
  reasoning_types: Record<string, string> | null
  expected_ids: string[]
}

describe('Mulberry32 matches the canonical JS reference (per enterprise_sim.evals.sample docstring)', () => {
  it('seed 1 draws the documented sequence', () => {
    const rng = new Mulberry32(1)
    const got = [rng.next(), rng.next(), rng.next(), rng.next(), rng.next()]
    expect(got).toEqual([0.6270739405881613, 0.002735721180215478, 0.5274470399599522, 0.9810509674716741, 0.9683778982143849])
  })

  it('is deterministic for a fixed seed', () => {
    const a = Array.from({ length: 20 }, () => new Mulberry32(42).next())
    const b = Array.from({ length: 20 }, () => new Mulberry32(42).next())
    expect(a).toEqual(b)
  })

  it('draws stay in [0, 1)', () => {
    const rng = new Mulberry32(12345)
    for (let i = 0; i < 1000; i++) {
      const v = rng.next()
      expect(v).toBeGreaterThanOrEqual(0)
      expect(v).toBeLessThan(1)
    }
  })
})

describe('seededShuffle / sampleIds / sample parity with enterprise_sim.evals.sample', () => {
  const data = JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: Case[] }
  expect(data.cases.length).toBeGreaterThanOrEqual(5)

  for (const c of data.cases) {
    it(`matches the Python reference: ${c.name}`, () => {
      const got = c.stratify
        ? sample(
            c.ids.map((id) => ({ id, reasoning_type: c.reasoning_types![id] })),
            { fraction: c.fraction, seed: c.seed, stratify: true }
          )
        : sampleIds(c.ids, { fraction: c.fraction, seed: c.seed })
      expect(got).toEqual(c.expected_ids)
    })
  }
})

describe('sample.ts direct unit coverage', () => {
  it('seededShuffle always starts from sorted(ids), independent of caller order', () => {
    const a = seededShuffle(['c', 'a', 'b'], 3)
    const b = seededShuffle(['a', 'b', 'c'], 3)
    expect(a).toEqual(b)
  })

  it('fraction <= 0 samples nothing', () => {
    expect(sampleIds(['a', 'b'], { fraction: 0, seed: 1 })).toEqual([])
    expect(sampleIds(['a', 'b'], { fraction: -1, seed: 1 })).toEqual([])
  })

  it('fraction >= 1 samples everything, sorted', () => {
    expect(sampleIds(['c', 'a', 'b'], { fraction: 1, seed: 1 })).toEqual(['a', 'b', 'c'])
  })

  it('stratified sampling seeds fresh per reasoning_type (one type absent never perturbs another)', () => {
    const withBoth = sample(
      [
        { id: 'a1', reasoning_type: 'direct_relation' },
        { id: 'a2', reasoning_type: 'direct_relation' },
        { id: 'b1', reasoning_type: 'transitive' }
      ],
      { fraction: 1, seed: 7, stratify: true }
    )
    const onlyA = sample(
      [
        { id: 'a1', reasoning_type: 'direct_relation' },
        { id: 'a2', reasoning_type: 'direct_relation' }
      ],
      { fraction: 1, seed: 7, stratify: true }
    )
    expect(withBoth.filter((id) => id.startsWith('a'))).toEqual(onlyA)
  })
})
