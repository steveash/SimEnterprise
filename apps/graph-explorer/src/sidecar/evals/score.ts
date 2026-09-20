// Set-based P/R/F1 scoring — a direct TS port of enterprise_sim/benchmark/score.py
// (`score_item` + macro aggregation), tested for parity against
// tests/fixtures/evals_score_parity.json (EXPLORER_EVALS.md §3).
import type { Aggregate, ItemScore, QAPair, Report } from '../../shared/evals-protocol.js'

/** Set precision/recall/F1 for one item (mirrors score.py's `_prf`). */
function prf(expected: Set<string>, predicted: Set<string>): { precision: number; recall: number; f1: number } {
  let truePositives = 0
  for (const id of expected) if (predicted.has(id)) truePositives++
  const precision = predicted.size ? truePositives / predicted.size : expected.size ? 0 : 1
  const recall = expected.size ? truePositives / expected.size : predicted.size ? 0 : 1
  const denom = precision + recall
  const f1 = denom ? (2 * precision * recall) / denom : 0
  return { precision, recall, f1 }
}

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const x of a) if (!b.has(x)) return false
  return true
}

/** Grade one question against a predicted id set (mirrors score.py's `score_item`). */
export function scoreItem(
  pair: Pick<QAPair, 'id' | 'reasoning_type' | 'expected_ids'>,
  predicted: Set<string>
): ItemScore {
  const expected = new Set(pair.expected_ids)
  const { precision, recall, f1 } = prf(expected, predicted)
  return {
    qa_id: pair.id,
    reasoning_type: pair.reasoning_type,
    expected: [...expected].sort(),
    predicted: [...predicted].sort(),
    exact_match: setsEqual(expected, predicted),
    precision,
    recall,
    f1
  }
}

/** Macro-average a group of items (mirrors score.py's `Aggregate.over`). */
export function aggregateOver(items: ItemScore[]): Aggregate {
  const n = items.length
  if (n === 0) return { count: 0, exact_match_rate: 0, macro_precision: 0, macro_recall: 0, macro_f1: 0 }
  let em = 0
  let p = 0
  let r = 0
  let f1 = 0
  for (const it of items) {
    if (it.exact_match) em++
    p += it.precision
    r += it.recall
    f1 += it.f1
  }
  return { count: n, exact_match_rate: em / n, macro_precision: p / n, macro_recall: r / n, macro_f1: f1 / n }
}

/**
 * Grade a predicted-id map against `pairs`, in `pairs` order, and macro-aggregate
 * overall + per reasoning_type (mirrors score.py's `score`). A pair with no entry
 * in `predictedById` is graded against the empty set.
 */
export function scoreReport(
  pairs: Pick<QAPair, 'id' | 'reasoning_type' | 'expected_ids'>[],
  predictedById: Map<string, string[]>
): Report {
  const items = pairs.map((pair) => scoreItem(pair, new Set(predictedById.get(pair.id) ?? [])))
  const grouped = new Map<string, ItemScore[]>()
  for (const item of items) {
    const arr = grouped.get(item.reasoning_type)
    if (arr) arr.push(item)
    else grouped.set(item.reasoning_type, [item])
  }
  const by_reasoning_type: Record<string, Aggregate> = {}
  for (const t of [...grouped.keys()].sort()) by_reasoning_type[t] = aggregateOver(grouped.get(t)!)
  return { items, overall: aggregateOver(items), by_reasoning_type }
}
