// Parity: src/sidecar/evals/score.ts against tests/fixtures/evals_score_parity.json,
// generated from enterprise_sim.benchmark.score.score_item (EXPLORER_EVALS.md §3/§6).
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { scoreItem, aggregateOver, scoreReport } from '../src/sidecar/evals/score.js'

const FIXTURE = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'tests', 'fixtures', 'evals_score_parity.json')

interface Case {
  name: string
  expected_ids: string[]
  predicted_ids: string[]
  precision: number
  recall: number
  f1: number
  exact_match: boolean
}

describe('scoreItem parity with enterprise_sim.benchmark.score.score_item', () => {
  const data = JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: Case[] }
  expect(data.cases.length).toBeGreaterThanOrEqual(5)

  for (const c of data.cases) {
    it(`matches the Python reference: ${c.name}`, () => {
      const item = scoreItem(
        { id: 'qa-x', reasoning_type: 'direct_relation', expected_ids: c.expected_ids },
        new Set(c.predicted_ids)
      )
      expect(item.precision).toBe(c.precision)
      expect(item.recall).toBe(c.recall)
      expect(item.f1).toBe(c.f1)
      expect(item.exact_match).toBe(c.exact_match)
    })
  }
})

describe('scoreItem / aggregateOver / scoreReport (direct unit coverage)', () => {
  it('sorts expected/predicted and is order-independent', () => {
    const item = scoreItem({ id: 'x', reasoning_type: 'transitive', expected_ids: ['b', 'a'] }, new Set(['a', 'b']))
    expect(item.expected).toEqual(['a', 'b'])
    expect(item.predicted).toEqual(['a', 'b'])
    expect(item.exact_match).toBe(true)
  })

  it('aggregateOver of an empty list is all zeros with count 0', () => {
    expect(aggregateOver([])).toEqual({ count: 0, exact_match_rate: 0, macro_precision: 0, macro_recall: 0, macro_f1: 0 })
  })

  it('aggregateOver macro-averages equally regardless of set size', () => {
    const items = [
      scoreItem({ id: '1', reasoning_type: 'direct_relation', expected_ids: ['a'] }, new Set(['a'])), // f1=1
      scoreItem({ id: '2', reasoning_type: 'direct_relation', expected_ids: ['a', 'b'] }, new Set()) // f1=0
    ]
    const agg = aggregateOver(items)
    expect(agg.count).toBe(2)
    expect(agg.macro_f1).toBeCloseTo(0.5)
    expect(agg.exact_match_rate).toBeCloseTo(0.5)
  })

  it('scoreReport grades a missing prediction against the empty set and groups by reasoning_type', () => {
    const pairs = [
      { id: 'q1', reasoning_type: 'direct_relation', expected_ids: ['a'] },
      { id: 'q2', reasoning_type: 'aggregation', expected_ids: ['b', 'c'] }
    ]
    const predicted = new Map([['q1', ['a']]]) // q2 unanswered
    const report = scoreReport(pairs, predicted)
    expect(report.items).toHaveLength(2)
    expect(report.items.find((i) => i.qa_id === 'q2')!.predicted).toEqual([])
    expect(report.overall.count).toBe(2)
    expect(Object.keys(report.by_reasoning_type).sort()).toEqual(['aggregation', 'direct_relation'])
    expect(report.by_reasoning_type.direct_relation.macro_f1).toBe(1)
    expect(report.by_reasoning_type.aggregation.macro_f1).toBe(0)
  })
})
