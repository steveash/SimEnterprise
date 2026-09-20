// On-disk conventions for a run's `evals/` directory (docs/EXPLORER.md §4,
// EXPLORER_EVALS.md §1/§3). Pure fs helpers — the questions.jsonl reader here is
// a direct parse (not a Python spawn) so hot renderer paths (sampling, running,
// browsing results) don't pay a subprocess round trip for something this simple;
// `evalsList` (index.ts) still goes through the Python CLI per the design doc,
// since it owns the counts and canonical JSON shape.
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { EvalExecutionMeta, EvalItemResult, QAPair } from '../../shared/evals-protocol.js'

export function evalsDir(runPath: string): string {
  return join(runPath, 'evals')
}

export function questionsPath(runPath: string): string {
  return join(evalsDir(runPath), 'questions.jsonl')
}

export function resultsDir(runPath: string): string {
  return join(evalsDir(runPath), 'results')
}

export function resultDir(runPath: string, evalId: string): string {
  return join(resultsDir(runPath), evalId)
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
export function readQuestions(runPath: string): QAPair[] {
  const p = questionsPath(runPath)
  if (!existsSync(p)) return []
  return parseJsonl<QAPair>(readFileSync(p, 'utf8'))
}

export function hasQuestions(runPath: string): boolean {
  return existsSync(questionsPath(runPath))
}

/** List `<run>/evals/results/*`'s eval ids, newest first (lexicographic — the id starts with a timestamp). */
export function listResultIds(runPath: string): string[] {
  const dir = resultsDir(runPath)
  if (!existsSync(dir)) return []
  return readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name)
    .sort()
    .reverse()
}

export function readEvalMeta(runPath: string, evalId: string): EvalExecutionMeta | null {
  const p = join(resultDir(runPath, evalId), 'eval.json')
  if (!existsSync(p)) return null
  return JSON.parse(readFileSync(p, 'utf8')) as EvalExecutionMeta
}

export function writeEvalMeta(runPath: string, meta: EvalExecutionMeta): void {
  const dir = resultDir(runPath, meta.eval_id)
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'eval.json'), JSON.stringify(meta, null, 2) + '\n', 'utf8')
}

export function readItems(runPath: string, evalId: string): EvalItemResult[] {
  const p = join(resultDir(runPath, evalId), 'items.jsonl')
  if (!existsSync(p)) return []
  return parseJsonl<EvalItemResult>(readFileSync(p, 'utf8'))
}

export function writeItems(runPath: string, evalId: string, items: EvalItemResult[]): void {
  const dir = resultDir(runPath, evalId)
  mkdirSync(dir, { recursive: true })
  writeJsonl(join(dir, 'items.jsonl'), items)
}

export function writePredictions(runPath: string, evalId: string, predictedById: Map<string, string[]>): void {
  const dir = resultDir(runPath, evalId)
  mkdirSync(dir, { recursive: true })
  const rows = [...predictedById.entries()].map(([qa_id, predicted_ids]) => ({ qa_id, predicted_ids }))
  writeJsonl(join(dir, 'predictions.jsonl'), rows)
}

export function predictionsPath(runPath: string, evalId: string): string {
  return join(resultDir(runPath, evalId), 'predictions.jsonl')
}

/** Drop a byproduct file (e.g. the Python runner's own `results.json`) that the sidecar's own eval.json supersedes. */
export function removeResultFile(runPath: string, evalId: string, name: string): void {
  try {
    rmSync(join(resultDir(runPath, evalId), name), { force: true })
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
