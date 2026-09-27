// On-disk conventions for a run's `evals/` directory (docs/EXPLORER.md §4,
// EXPLORER_EVALS.md §1/§3). Pure fs helpers — the questions.jsonl reader here is
// a direct parse (not a Python spawn) so hot renderer paths (sampling, running,
// browsing results) don't pay a subprocess round trip for something this simple;
// `evalsList` (index.ts) still goes through the Python CLI per the design doc,
// since it owns the counts and canonical JSON shape.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { EvalExecutionMeta, EvalItemResult, EvalTrace, QAPair } from '../../shared/evals-protocol.js'

export function evalsDir(worldPath: string): string {
  return join(worldPath, 'evals')
}

export function questionsPath(worldPath: string): string {
  return join(evalsDir(worldPath), 'questions.jsonl')
}

export function resultsDir(worldPath: string): string {
  return join(evalsDir(worldPath), 'results')
}

export function resultDir(worldPath: string, evalId: string): string {
  return join(resultsDir(worldPath), evalId)
}

function parseJsonl<T>(text: string): T[] {
  const out: T[] = []
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t) continue
    out.push(JSON.parse(t) as T)
  }
  return out
}

function writeJsonl(path: string, rows: unknown[]): void {
  const text = rows.map((r) => JSON.stringify(r)).join('\n') + (rows.length ? '\n' : '')
  writeFileSync(path, text, 'utf8')
}

/** Read `<run>/evals/questions.jsonl`; `[]` if the question set hasn't been generated yet. */
export function readQuestions(worldPath: string): QAPair[] {
  const p = questionsPath(worldPath)
  if (!existsSync(p)) return []
  return parseJsonl<QAPair>(readFileSync(p, 'utf8'))
}

export function hasQuestions(worldPath: string): boolean {
  return existsSync(questionsPath(worldPath))
}

/** List `<run>/evals/results/*`'s eval ids, newest first (lexicographic — the id starts with a timestamp). */
export function listResultIds(worldPath: string): string[] {
  const dir = resultsDir(worldPath)
  if (!existsSync(dir)) return []
  return readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name)
    .sort()
    .reverse()
}

export function readEvalMeta(worldPath: string, evalId: string): EvalExecutionMeta | null {
  const p = join(resultDir(worldPath, evalId), 'eval.json')
  if (!existsSync(p)) return null
  return JSON.parse(readFileSync(p, 'utf8')) as EvalExecutionMeta
}

export function writeEvalMeta(worldPath: string, meta: EvalExecutionMeta): void {
  const dir = resultDir(worldPath, meta.eval_id)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'eval.json'), JSON.stringify(meta, null, 2) + '\n', 'utf8')
}

export function readItems(worldPath: string, evalId: string): EvalItemResult[] {
  const p = join(resultDir(worldPath, evalId), 'items.jsonl')
  if (!existsSync(p)) return []
  return parseJsonl<EvalItemResult>(readFileSync(p, 'utf8'))
}

export function writeItems(worldPath: string, evalId: string, items: EvalItemResult[]): void {
  const dir = resultDir(worldPath, evalId)
  mkdirSync(dir, { recursive: true })
  writeJsonl(join(dir, 'items.jsonl'), items)
}

export function writePredictions(worldPath: string, evalId: string, predictedById: Map<string, string[]>): void {
  const dir = resultDir(worldPath, evalId)
  mkdirSync(dir, { recursive: true })
  const rows = [...predictedById.entries()].map(([qa_id, predicted_ids]) => ({ qa_id, predicted_ids }))
  writeJsonl(join(dir, 'predictions.jsonl'), rows)
}

export function predictionsPath(worldPath: string, evalId: string): string {
  return join(resultDir(worldPath, evalId), 'predictions.jsonl')
}

/** Drop a byproduct file (e.g. the Python runner's own `results.json`) that the sidecar's own eval.json supersedes. */
export function removeResultFile(worldPath: string, evalId: string, name: string): void {
  try {
    rmSync(join(resultDir(worldPath, evalId), name), { force: true })
  } catch {
    /* best-effort cleanup */
  }
}

export function makeEvalId(now: Date, runner: string, n: number): string {
  const pad = (x: number, len = 2) => String(x).padStart(len, '0')
  const stamp =
    `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}` +
    `-${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`
  return `${stamp}-${runner}-${n}q`
}

/** `<world>/evals/results/<eval-id>/traces/` — one JSON transcript per answered question. */
export function tracesDir(worldPath: string, evalId: string): string {
  return join(resultDir(worldPath, evalId), 'traces')
}

function traceFile(worldPath: string, evalId: string, qaId: string): string {
  // qa ids are content hashes (`qa-<hex>`); keep the file name safe regardless.
  return join(tracesDir(worldPath, evalId), `${qaId.replace(/[^A-Za-z0-9._-]/g, '_')}.json`)
}

export function writeTrace(worldPath: string, trace: EvalTrace): void {
  mkdirSync(tracesDir(worldPath, trace.eval_id), { recursive: true })
  writeFileSync(traceFile(worldPath, trace.eval_id, trace.qa_id), JSON.stringify(trace) + '\n', 'utf8')
}

export function readTrace(worldPath: string, evalId: string, qaId: string): EvalTrace | null {
  const p = traceFile(worldPath, evalId, qaId)
  if (!existsSync(p)) return null
  return JSON.parse(readFileSync(p, 'utf8')) as EvalTrace
}

/** The qa ids that have a persisted transcript in this execution. */
export function listTraced(worldPath: string, evalId: string): string[] {
  const dir = tracesDir(worldPath, evalId)
  if (!existsSync(dir)) return []
  return readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .map((f) => f.slice(0, -'.json'.length))
}
