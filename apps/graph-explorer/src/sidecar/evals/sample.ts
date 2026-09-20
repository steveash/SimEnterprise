// Deterministic eval-question sampling — a literal TS port of
// enterprise_sim/evals/sample.py (EXPLORER_EVALS.md §3). Tested for bit-for-bit
// parity against tests/fixtures/evals_sample_parity.json, generated from the
// Python reference.
//
// Two things are ported exactly, per the Python module's docstring:
//
// 1. mulberry32 — a tiny public-domain 32-bit PRNG. `Mulberry32.next()` below is
//    a literal transcription of the canonical JS reference (the same reference
//    the Python port itself transcribes), not a "cleaned up" rewrite.
// 2. seeded Fisher-Yates over `sorted(ids)`, driven by one Mulberry32 stream —
//    see `seededShuffle`/`sampleIds`/`sample` below, which mirror their Python
//    namesakes line for line.

/** A 32-bit deterministic PRNG (public-domain "mulberry32"), literally transcribed
 * from the canonical JS reference so the same seed draws the identical sequence
 * as the Python port (`enterprise_sim.evals.sample.Mulberry32`). */
export class Mulberry32 {
  private a: number

  constructor(seed: number) {
    this.a = seed | 0
  }

  /** A uniform value in [0, 1) — advances the generator one step. */
  next(): number {
    this.a |= 0
    this.a = (this.a + 0x6d2b79f5) | 0
    let t = Math.imul(this.a ^ (this.a >>> 15), 1 | this.a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** A deterministic Fisher-Yates shuffle of `sorted(ids)`, seeded by {@link Mulberry32}. */
export function seededShuffle(ids: Iterable<string>, seed: number): string[] {
  const items = [...ids].sort()
  const rng = new Mulberry32(seed)
  for (let i = items.length - 1; i > 0; i--) {
    const j = Math.floor(rng.next() * (i + 1))
    const tmp = items[i]
    items[i] = items[j]
    items[j] = tmp
  }
  return items
}

/** The first `ceil(fraction * n)` ids of a seeded shuffle of `sorted(ids)`, sorted. */
export function sampleIds(ids: Iterable<string>, opts: { fraction: number; seed: number }): string[] {
  const items = [...ids]
  const n = items.length
  if (n === 0 || opts.fraction <= 0) return []
  if (opts.fraction >= 1) return [...items].sort()
  const k = Math.ceil(opts.fraction * n)
  return seededShuffle(items, opts.seed).slice(0, k).sort()
}

/** Minimal shape `sample` needs from a question — anything with `id`/`reasoning_type` works. */
export interface Sampleable {
  id: string
  reasoning_type: string
}

/**
 * Deterministically sample a `fraction` of `pairs`' ids. When `stratify` is set,
 * groups by `reasoning_type` (sorted-type order) and samples each group
 * independently with a *fresh* `Mulberry32(seed)` — mirrors
 * `enterprise_sim.evals.sample.sample`.
 */
export function sample(pairs: Sampleable[], opts: { fraction: number; seed: number; stratify?: boolean }): string[] {
  if (!opts.stratify) {
    return sampleIds(
      pairs.map((p) => p.id),
      opts
    )
  }
  const byType = new Map<string, string[]>()
  for (const pair of pairs) {
    const arr = byType.get(pair.reasoning_type)
    if (arr) arr.push(pair.id)
    else byType.set(pair.reasoning_type, [pair.id])
  }
  const result: string[] = []
  for (const t of [...byType.keys()].sort()) {
    result.push(...sampleIds(byType.get(t)!, opts))
  }
  return result.sort()
}
