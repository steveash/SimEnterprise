// Eval execution (EXPLORER_EVALS.md §3): the `evalsRun` orchestrator. `runner:
// 'explorer'` answers in-process with a small bounded promise pool (1-4);
// `'rag'`/`'graph'` spawn `enterprise-sim evals run` through the Python bridge
// and re-emit its JSONL events in the same stream shape. Either way this module
// persists exactly `eval.json` / `predictions.jsonl` / `items.jsonl` under
// `<run>/evals/results/<eval-id>/`.
import { existsSync, mkdirSync } from 'node:fs'
import type { ChatEngines } from '../agent/harness.js'
import { streamJsonl } from '../python.js'
import { answerQuestion, DEFAULT_MAX_TURNS, type AnswerResult } from './answerer.js'
import { scoreItem, scoreReport } from './score.js'
import {
  predictionsPath,
  removeResultFile,
  resultDir,
  writeEvalMeta,
  writeItems,
  writePredictions
} from './store.js'
import type {
  EvalExecutionMeta,
  EvalItemResult,
  EvalSelection,
  EvalStatus,
  EvalStreamEvent,
  QAPair,
  Report,
  TraceEntry
} from '../../shared/evals-protocol.js'
import type { AgentEvent } from '../../shared/agent-events.js'

/** Fold a turn's tool_use/tool_result AgentEvents into the compact trace the UI shows. */
function traceFromEvents(events: AgentEvent[]): TraceEntry[] {
  const entries = new Map<string, TraceEntry>()
  const order: string[] = []
  for (const e of events) {
    if (e.kind === 'tool_use') {
      entries.set(e.id, { id: e.id, name: e.name, input: e.input })
      order.push(e.id)
    } else if (e.kind === 'tool_result') {
      const entry = entries.get(e.id)
      if (entry) {
        entry.ok = e.ok
        entry.preview = e.preview
      }
    }
  }
  return order.map((id) => entries.get(id)!).filter(Boolean)
}

/** Injectable so tests can stub the answerer without a real Agent SDK call. */
export type AnswerFn = (engines: ChatEngines, opts: Parameters<typeof answerQuestion>[1]) => Promise<AnswerResult>

export interface RunExplorerOpts {
  model: string | null
  concurrency: number
  signal: AbortSignal
  selection: EvalSelection
  stream: (e: EvalStreamEvent) => void
  answerFn?: AnswerFn
}

export async function runEvalExplorer(
  engines: ChatEngines,
  runPath: string,
  evalId: string,
  pairs: QAPair[],
  opts: RunExplorerOpts
): Promise<EvalExecutionMeta> {
  const answerFn = opts.answerFn ?? answerQuestion
  const started_at = new Date().toISOString()
  const predictedById = new Map<string, string[]>()
  const items: EvalItemResult[] = []
  const completedPairs: QAPair[] = []
  const usages: unknown[] = []
  let doneCount = 0
  let totalCost = 0

  let next = 0
  async function worker(): Promise<void> {
    for (;;) {
      if (opts.signal.aborted) return
      const i = next++
      if (i >= pairs.length) return
      const pair = pairs[i]
      opts.stream({ kind: 'question_started', id: pair.id })
      const res = await answerFn(engines, {
        question: pair.question,
        model: opts.model ?? undefined,
        maxTurns: DEFAULT_MAX_TURNS,
        signal: opts.signal
      })
      const scored = scoreItem(pair, new Set(res.predicted_ids))
      predictedById.set(pair.id, res.predicted_ids)
      completedPairs.push(pair)
      items.push({
        qa_id: pair.id,
        question: pair.question,
        reasoning_type: pair.reasoning_type,
        expected: scored.expected,
        predicted: scored.predicted,
        precision: scored.precision,
        recall: scored.recall,
        f1: scored.f1,
        exact_match: scored.exact_match,
        turns: res.turns,
        engine: res.engine,
        query: res.query,
        seconds: res.seconds,
        cost_usd: res.cost_usd,
        error: res.error
      })
      if (typeof res.cost_usd === 'number') totalCost += res.cost_usd
      if (res.usage) usages.push(res.usage)
      doneCount++
      opts.stream({
        kind: 'question_done',
        id: pair.id,
        predicted_ids: res.predicted_ids,
        expected_ids: pair.expected_ids,
        precision: scored.precision,
        recall: scored.recall,
        f1: scored.f1,
        exact: scored.exact_match,
        engine: res.engine,
        query: res.query,
        seconds: res.seconds,
        cost_usd: res.cost_usd,
        // A single-question run doubles as the "run one question live" UI action
        // (EXPLORER_EVALS.md §3/§5) — include the trace only then, so a batch run's
        // stream stays light.
        trace: pairs.length === 1 ? traceFromEvents(res.events) : undefined
      })
      const soFar = scoreReport(completedPairs, predictedById)
      opts.stream({
        kind: 'progress',
        done: doneCount,
        total: pairs.length,
        macro_f1_so_far: soFar.overall.macro_f1,
        by_reasoning_type: soFar.by_reasoning_type
      })
    }
  }

  const workers = Array.from({ length: Math.max(1, Math.min(opts.concurrency, pairs.length)) }, () => worker())
  await Promise.all(workers)

  const cancelled = opts.signal.aborted
  const reportPairs = cancelled ? completedPairs : pairs
  const report = scoreReport(reportPairs, predictedById)

  writeItems(runPath, evalId, items)
  writePredictions(runPath, evalId, predictedById)

  const meta: EvalExecutionMeta = {
    eval_id: evalId,
    runner: 'explorer',
    model: opts.model,
    selection: opts.selection,
    question_ids: pairs.map((p) => p.id),
    started_at,
    finished_at: new Date().toISOString(),
    status: cancelled ? 'cancelled' : 'done',
    cost_usd: totalCost > 0 ? totalCost : undefined,
    usage: usages.length ? usages : undefined,
    report
  }
  writeEvalMeta(runPath, meta)
  opts.stream({ kind: 'done', report })
  return meta
}

export interface RunPythonOpts {
  runner: 'rag' | 'graph'
  model: string | null
  signal: AbortSignal
  stream: (e: EvalStreamEvent) => void
}

/** Loose shape of the `enterprise-sim evals run` JSONL events (evals/runner.py). */
interface PythonEvalEvent {
  kind: string
  id?: string
  predicted_ids?: string[]
  expected_ids?: string[]
  precision?: number
  recall?: number
  f1?: number
  exact?: boolean
  report?: Report
  error?: string
}

export async function runEvalPython(
  repoRoot: string,
  runPath: string,
  evalId: string,
  pairs: QAPair[],
  selection: EvalSelection,
  opts: RunPythonOpts
): Promise<EvalExecutionMeta> {
  const started_at = new Date().toISOString()
  const dir = resultDir(runPath, evalId)
  mkdirSync(dir, { recursive: true })

  const args = ['evals', 'run', '--run', runPath, '--runner', opts.runner, '--out', dir]
  if (selection.kind === 'ids') {
    args.push('--ids', ...selection.ids)
  } else if (selection.kind === 'fraction') {
    args.push('--fraction', String(selection.fraction), '--seed', String(selection.seed))
    if (selection.stratify) args.push('--stratify')
  }
  if (opts.model) args.push('--model', opts.model)

  const pending = [...pairs]
  const predictedById = new Map<string, string[]>()
  const completedPairs: QAPair[] = []
  let doneCount = 0
  let pythonReport: Report | null = null
  let errorMsg: string | null = null

  if (pending.length) opts.stream({ kind: 'question_started', id: pending[0].id })

  const handle = streamJsonl(
    repoRoot,
    args,
    (obj) => {
      const evt = obj as PythonEvalEvent
      if (evt.kind === 'question' && evt.id) {
        const id = evt.id
        const predicted_ids = evt.predicted_ids ?? []
        const pair = pairs.find((p) => p.id === id)
        predictedById.set(id, predicted_ids)
        if (pair) completedPairs.push(pair)
        doneCount++
        opts.stream({
          kind: 'question_done',
          id,
          predicted_ids,
          expected_ids: evt.expected_ids ?? pair?.expected_ids ?? [],
          precision: evt.precision ?? 0,
          recall: evt.recall ?? 0,
          f1: evt.f1 ?? 0,
          exact: evt.exact ?? false
        })
        const idx = pending.findIndex((p) => p.id === id)
        if (idx >= 0) pending.splice(idx, 1)
        if (pending.length) opts.stream({ kind: 'question_started', id: pending[0].id })
        const soFar = scoreReport(completedPairs, predictedById)
        opts.stream({
          kind: 'progress',
          done: doneCount,
          total: pairs.length,
          macro_f1_so_far: soFar.overall.macro_f1,
          by_reasoning_type: soFar.by_reasoning_type
        })
      } else if (evt.kind === 'done' && evt.report) {
        pythonReport = evt.report
      } else if (evt.kind === 'error') {
        errorMsg = evt.error ?? 'runner error'
      }
    },
    { cwd: repoRoot }
  )

  const abortListener = () => handle.kill()
  opts.signal.addEventListener('abort', abortListener)
  const code = await handle.done
  opts.signal.removeEventListener('abort', abortListener)

  const cancelled = opts.signal.aborted

  const items: EvalItemResult[] = completedPairs.map((pair) => {
    const scored = scoreItem(pair, new Set(predictedById.get(pair.id) ?? []))
    return {
      qa_id: pair.id,
      question: pair.question,
      reasoning_type: pair.reasoning_type,
      expected: scored.expected,
      predicted: scored.predicted,
      precision: scored.precision,
      recall: scored.recall,
      f1: scored.f1,
      exact_match: scored.exact_match,
      seconds: 0
    }
  })
  writeItems(runPath, evalId, items)
  // `enterprise-sim evals run --out <dir>` already wrote predictions.jsonl in the
  // exact {qa_id, predicted_ids} shape; only synthesize one if it didn't get that far.
  if (!existsSync(predictionsPath(runPath, evalId))) writePredictions(runPath, evalId, predictedById)
  removeResultFile(runPath, evalId, 'results.json') // superseded by eval.json

  const report: Report = pythonReport ?? scoreReport(cancelled ? completedPairs : pairs, predictedById)
  const status: EvalStatus = errorMsg ? 'failed' : cancelled ? 'cancelled' : code === 0 ? 'done' : 'failed'

  const meta: EvalExecutionMeta = {
    eval_id: evalId,
    runner: opts.runner,
    model: opts.model,
    selection,
    question_ids: pairs.map((p) => p.id),
    started_at,
    finished_at: new Date().toISOString(),
    status,
    report
  }
  writeEvalMeta(runPath, meta)
  if (errorMsg) opts.stream({ kind: 'error', message: errorMsg })
  opts.stream({ kind: 'done', report })
  return meta
}
