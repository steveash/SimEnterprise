// Evals feature ops (docs/EXPLORER_EVALS.md). Registered onto the sidecar's
// feature-op dispatcher (ops.ts) by importing this module from features.ts.
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { registerOps, type OpContext } from '../ops.js'
import { runJson } from '../python.js'
import { sample } from './sample.js'
import { runPropose } from './propose.js'
import { runEvalExplorer, runEvalPython } from './run.js'
import {
  hasQuestions,
  listResultIds,
  makeEvalId,
  predictionsPath,
  readEvalMeta,
  readItems,
  readQuestions,
  writeEvalMeta
} from './store.js'
import type {
  EvalExecutionMeta,
  EvalSelection,
  EvalsListResult,
  QAPair,
  QuestionCounts
} from '../../shared/evals-protocol.js'

const runAborts = new Map<string, AbortController>() // evalsRun request id -> controller
const proposeAborts = new Map<string, AbortController>() // evalsPropose request id -> controller
const proposeSessions = new Map<string, string>() // conversationId -> sdk session id

function clamp(n: number, lo: number, hi: number): number {
  if (!Number.isFinite(n)) return lo
  return Math.min(hi, Math.max(lo, n))
}

function parseSelection(raw: unknown): EvalSelection {
  if (raw == null || raw === 'all') return { kind: 'all' }
  const r = raw as Record<string, unknown>
  if (r.kind === 'ids' || Array.isArray(r.ids)) {
    return { kind: 'ids', ids: Array.isArray(r.ids) ? (r.ids as string[]) : [] }
  }
  if (r.kind === 'fraction' || typeof r.fraction === 'number') {
    return {
      kind: 'fraction',
      fraction: Number(r.fraction),
      seed: Number(r.seed ?? 0),
      stratify: Boolean(r.stratify)
    }
  }
  return { kind: 'all' }
}

function resolveSelection(pairs: QAPair[], selection: EvalSelection): QAPair[] {
  if (selection.kind === 'all') return pairs
  if (selection.kind === 'ids') {
    const wanted = new Set(selection.ids)
    return pairs.filter((p) => wanted.has(p.id))
  }
  const ids = new Set(sample(pairs, { fraction: selection.fraction, seed: selection.seed, stratify: selection.stratify }))
  return pairs.filter((p) => ids.has(p.id))
}

registerOps({
  /** Derive & write `<run>/evals/questions.jsonl` from the run's gold KG (`evals generate`). */
  evalsGenerate: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    const force = Boolean(ctx.params.force)
    return runJson(ctx.repoRoot, ['evals', 'generate', '--run', runPath, ...(force ? ['--force'] : [])])
  },

  /** The run's question set + counts (via `evals list`) plus expected_ids -> label, resolved from the loaded model. */
  evalsList: async (ctx: OpContext): Promise<EvalsListResult> => {
    const runPath = ctx.params.runPath as string
    if (!hasQuestions(runPath)) return { questions: [], counts: emptyCounts(), expected_labels: {} }
    const raw = await runJson<{ questions: QAPair[]; counts: QuestionCounts }>(ctx.repoRoot, [
      'evals',
      'list',
      '--run',
      runPath
    ])
    const expected_labels: Record<string, string> = {}
    try {
      const lr = await ctx.ensureLoaded(runPath)
      for (const q of raw.questions) {
        for (const id of q.expected_ids) {
          if (id in expected_labels) continue
          const node = lr.index.getNode(id)
          if (node) expected_labels[id] = node.label
        }
      }
    } catch {
      /* run not loadable — return without labels rather than fail the whole list */
    }
    return { questions: raw.questions, counts: raw.counts, expected_labels }
  },

  /** Validate + append AI-proposed questions (`evals add`); the Python side re-validates and assigns ids. */
  evalsAdd: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    const proposals = (ctx.params.proposals as unknown[]) ?? []
    const dir = mkdtempSync(join(tmpdir(), 'esim-evals-add-'))
    try {
      const file = join(dir, 'proposals.json')
      writeFileSync(file, JSON.stringify(proposals), 'utf8')
      return await runJson(ctx.repoRoot, ['evals', 'add', '--run', runPath, '--file', file])
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  },

  /** Remove proposed questions by id (`evals remove`; generated questions cannot be removed). */
  evalsRemove: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    const ids = (ctx.params.ids as string[]) ?? []
    if (ids.length === 0) return { removed: [], not_removed: [] }
    return runJson(ctx.repoRoot, ['evals', 'remove', '--run', runPath, '--ids', ...ids])
  },

  /** Deterministic sample of question ids (TS port, sample.ts — parity-tested against the Python reference). */
  evalsSample: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    const fraction = Number(ctx.params.fraction)
    const seed = Number(ctx.params.seed ?? 0)
    const stratify = Boolean(ctx.params.stratify)
    const questions = readQuestions(runPath)
    const ids = sample(questions, { fraction, seed, stratify })
    return { ids, count: ids.length }
  },

  /** Run a selection of questions with a runner, streaming progress (EXPLORER_EVALS.md §3). */
  evalsRun: async (ctx: OpContext): Promise<EvalExecutionMeta> => {
    const runPath = ctx.params.runPath as string
    const runner = ((ctx.params.runner as string) ?? 'explorer') as 'explorer' | 'rag' | 'graph'
    const model = (ctx.params.model as string | undefined) ?? null
    const concurrency = clamp(Math.round(Number(ctx.params.concurrency ?? 2)), 1, 4)
    const selection = parseSelection(ctx.params.selection)

    const allPairs = readQuestions(runPath)
    if (allPairs.length === 0) {
      throw new Error('this run has no eval questions yet — generate the question set first')
    }
    const pairs = resolveSelection(allPairs, selection)
    if (pairs.length === 0) throw new Error('the selection matched no questions')

    const evalId = makeEvalId(new Date(), runner, pairs.length)
    const ac = new AbortController()
    runAborts.set(ctx.requestId, ac)
    writeEvalMeta(runPath, {
      eval_id: evalId,
      runner,
      model,
      selection,
      question_ids: pairs.map((p) => p.id),
      started_at: new Date().toISOString(),
      finished_at: null,
      status: 'running',
      report: null
    })

    try {
      if (runner === 'explorer') {
        const lr = await ctx.ensureLoaded(runPath)
        return await runEvalExplorer(lr, runPath, evalId, pairs, {
          model,
          concurrency,
          selection,
          signal: ac.signal,
          stream: (e) => ctx.stream(e)
        })
      }
      return await runEvalPython(ctx.repoRoot, runPath, evalId, pairs, selection, {
        runner,
        model,
        signal: ac.signal,
        stream: (e) => ctx.stream(e)
      })
    } finally {
      runAborts.delete(ctx.requestId)
    }
  },

  /** Cancel a running `evalsRun` by its request id. Finished questions are kept; the report covers them. */
  evalsCancel: async (ctx: OpContext) => {
    runAborts.get(ctx.params.id as string)?.abort()
    return { cancelled: true }
  },

  /** Every `evals/results/*` execution's metadata, newest first. */
  evalsListResults: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    return listResultIds(runPath)
      .map((id) => readEvalMeta(runPath, id))
      .filter((m): m is EvalExecutionMeta => m !== null)
  },

  /** One execution's metadata + per-question items + the predictions file path. */
  evalsReadResult: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    const evalId = ctx.params.evalId as string
    const meta = readEvalMeta(runPath, evalId)
    if (!meta) throw new Error(`no such eval result: ${evalId}`)
    return { meta, items: readItems(runPath, evalId), predictionsPath: predictionsPath(runPath, evalId) }
  },

  /** One proposal-chat turn (EXPLORER_EVALS.md §4), streaming AgentEvents + `{kind:'proposal', …}`. */
  evalsPropose: async (ctx: OpContext) => {
    const runPath = ctx.params.runPath as string
    const lr = await ctx.ensureLoaded(runPath)
    const convoKey = (ctx.params.conversationId as string) ?? ctx.requestId
    const ac = new AbortController()
    proposeAborts.set(ctx.requestId, ac)
    try {
      await runPropose(lr, {
        runPath,
        prompt: ctx.params.prompt as string,
        model: ctx.params.model as string | undefined,
        resume: proposeSessions.get(convoKey) ?? null,
        signal: ac.signal,
        onEvent: (e) => {
          if (e.kind === 'done' && e.sessionId) proposeSessions.set(convoKey, e.sessionId)
          ctx.stream(e)
        }
      })
    } finally {
      proposeAborts.delete(ctx.requestId)
    }
    return { finished: true }
  },

  /** Cancel a running `evalsPropose` turn by its request id. */
  evalsCancelPropose: async (ctx: OpContext) => {
    proposeAborts.get(ctx.params.id as string)?.abort()
    return { cancelled: true }
  }
})

function emptyCounts(): QuestionCounts {
  return { reasoning_type: {}, qtype: {}, difficulty: {}, source: {} }
}
