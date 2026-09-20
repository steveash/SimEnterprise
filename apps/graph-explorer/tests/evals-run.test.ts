// Eval-execution persistence (EXPLORER_EVALS.md §3/§6): `runEvalExplorer` with a
// stubbed answerer (no Agent SDK / no key needed) — asserts the stream event
// shapes and the on-disk eval.json/predictions.jsonl/items.jsonl layout. Also
// the end-to-end sidecar ops (evalsGenerate/evalsList/evalsSample/evalsRun)
// against the golden run, skipped when it isn't present.
import { describe, it, expect, beforeAll, beforeEach, afterEach } from 'vitest'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { runEvalExplorer } from '../src/sidecar/evals/run.js'
import { readEvalMeta, readItems, resultDir } from '../src/sidecar/evals/store.js'
import type { AnswerResult } from '../src/sidecar/evals/answerer.js'
import type { ChatEngines } from '../src/sidecar/agent/harness.js'
import type { EvalStreamEvent, QAPair } from '../src/shared/evals-protocol.js'
import { getOp } from '../src/sidecar/ops.js'
import '../src/sidecar/evals/index.js' // registers the evals* ops
import { goldenRunExists, GOLDEN_RUN } from './helpers.js'

const PAIRS: QAPair[] = [
  {
    id: 'qa-1',
    question: 'Who does Ash report to?',
    qtype: 'who',
    reasoning_type: 'direct_relation',
    expected_ids: ['person:manager'],
    expected_label: null,
    difficulty: 'easy',
    source: 'generated',
    tags: []
  },
  {
    id: 'qa-2',
    question: 'How many people are on Nitro?',
    qtype: 'count',
    reasoning_type: 'aggregation',
    expected_ids: ['person:ash', 'person:bo'],
    expected_label: '2',
    difficulty: 'medium',
    source: 'generated',
    tags: []
  }
]

/** A ChatEngines stub — runEvalExplorer never touches it directly (only the injected answerFn does). */
const FAKE_ENGINES = {} as ChatEngines

function stubAnswerer(byId: Record<string, string[]>): (engines: ChatEngines, opts: { question: string }) => Promise<AnswerResult> {
  return async (_engines, opts) => {
    const pair = PAIRS.find((p) => p.question === opts.question)!
    return { predicted_ids: byId[pair.id] ?? [], turns: 1, seconds: 0.01, events: [] }
  }
}

describe('runEvalExplorer persistence with a stubbed answerer', () => {
  let runPath: string

  beforeEach(() => {
    runPath = mkdtempSync(join(tmpdir(), 'esim-evals-run-'))
  })

  afterEach(() => {
    rmSync(runPath, { recursive: true, force: true })
  })

  it('writes eval.json, predictions.jsonl, items.jsonl and streams the documented event kinds', async () => {
    const events: EvalStreamEvent[] = []
    const ac = new AbortController()
    const meta = await runEvalExplorer(FAKE_ENGINES, runPath, '20260101-000000-explorer-2q', PAIRS, {
      model: 'sonnet',
      concurrency: 2,
      signal: ac.signal,
      selection: { kind: 'all' },
      stream: (e) => events.push(e),
      answerFn: stubAnswerer({ 'qa-1': ['person:manager'], 'qa-2': ['person:ash'] }) // qa-2: partial (misses person:bo)
    })

    expect(meta.status).toBe('done')
    expect(meta.eval_id).toBe('20260101-000000-explorer-2q')
    expect(meta.question_ids.sort()).toEqual(['qa-1', 'qa-2'])
    expect(meta.report).not.toBeNull()
    expect(meta.report!.overall.count).toBe(2)
    expect(meta.report!.items.find((i) => i.qa_id === 'qa-1')!.exact_match).toBe(true)
    expect(meta.report!.items.find((i) => i.qa_id === 'qa-2')!.f1).toBeCloseTo(2 / 3) // P=1 R=0.5

    const kinds = events.map((e) => e.kind)
    expect(kinds).toContain('question_started')
    expect(kinds).toContain('question_done')
    expect(kinds).toContain('progress')
    expect(kinds.at(-1)).toBe('done')
    const doneEvent = events.find((e) => e.kind === 'done')!
    expect(doneEvent.kind === 'done' && doneEvent.report.overall.count).toBe(2)

    const dir = resultDir(runPath, meta.eval_id)
    expect(existsSync(join(dir, 'eval.json'))).toBe(true)
    expect(existsSync(join(dir, 'predictions.jsonl'))).toBe(true)
    expect(existsSync(join(dir, 'items.jsonl'))).toBe(true)

    const diskMeta = readEvalMeta(runPath, meta.eval_id)
    expect(diskMeta?.status).toBe('done')
    expect(diskMeta?.report?.overall.count).toBe(2)

    const items = readItems(runPath, meta.eval_id)
    expect(items).toHaveLength(2)
    expect(items.find((i) => i.qa_id === 'qa-1')!.predicted).toEqual(['person:manager'])

    const predLines = readFileSync(join(dir, 'predictions.jsonl'), 'utf8').trim().split('\n')
    expect(predLines).toHaveLength(2)
    const preds = predLines.map((l) => JSON.parse(l) as { qa_id: string; predicted_ids: string[] })
    expect(preds.find((p) => p.qa_id === 'qa-1')?.predicted_ids).toEqual(['person:manager'])
  })

  it('a single-question run includes a per-tool trace on question_done', async () => {
    const events: EvalStreamEvent[] = []
    const ac = new AbortController()
    await runEvalExplorer(FAKE_ENGINES, runPath, '20260101-000000-explorer-1q', [PAIRS[0]], {
      model: 'sonnet',
      concurrency: 1,
      signal: ac.signal,
      selection: { kind: 'ids', ids: ['qa-1'] },
      stream: (e) => events.push(e),
      answerFn: async () => ({
        predicted_ids: ['person:manager'],
        turns: 1,
        seconds: 0.01,
        engine: 'Cypher',
        query: 'MATCH (p)-[:reports_to]->(m) RETURN m.id',
        events: [
          { kind: 'tool_use', id: 't1', name: 'cypher_query', input: { query: 'MATCH ...' } },
          { kind: 'tool_result', id: 't1', ok: true, preview: 'ok' }
        ]
      })
    })
    const done = events.find((e) => e.kind === 'question_done')
    expect(done).toBeDefined()
    expect(done!.kind === 'question_done' && done!.trace).toEqual([
      { id: 't1', name: 'cypher_query', input: { query: 'MATCH ...' }, ok: true, preview: 'ok' }
    ])
  })

  it('cancellation keeps finished questions and reports status "cancelled" over the completed subset', async () => {
    const ac = new AbortController()
    let answered = 0
    const meta = await runEvalExplorer(FAKE_ENGINES, runPath, '20260101-000000-explorer-2q', PAIRS, {
      model: 'sonnet',
      concurrency: 1,
      signal: ac.signal,
      selection: { kind: 'all' },
      stream: () => {},
      answerFn: async (_e, opts) => {
        answered++
        if (answered === 1) ac.abort() // abort right after the first question finishes
        const pair = PAIRS.find((p) => p.question === opts.question)!
        return { predicted_ids: pair.expected_ids, turns: 1, seconds: 0.01, events: [] }
      }
    })
    expect(meta.status).toBe('cancelled')
    expect(meta.report!.overall.count).toBe(1) // only the first question was scored
  })
})

describe.skipIf(!goldenRunExists())('evals ops end-to-end against the golden run', () => {
  // The golden run's question set is derived (not checked in); generate it once
  // through the real Python CLI if this checkout hasn't yet.
  beforeAll(async () => {
    if (existsSync(join(GOLDEN_RUN, 'evals', 'questions.jsonl'))) return
    const generate = getOp('evalsGenerate')!
    await generate({
      requestId: 'r0',
      params: { runPath: GOLDEN_RUN },
      ensureLoaded: async () => {
        throw new Error('not needed')
      },
      stream: () => {},
      runsRoot: '',
      repoRoot: join(GOLDEN_RUN, '..', '..', '..')
    })
  }, 120_000)

  it('evalsList reflects the golden run\'s generated question set with resolved labels', async () => {
    const handler = getOp('evalsList')
    expect(handler).toBeDefined()
    const result = (await handler!({
      requestId: 'r1',
      params: { runPath: GOLDEN_RUN },
      ensureLoaded: async (runPath: string) => {
        const { loadRun } = await import('../src/sidecar/graph/loader.js')
        const { GraphIndex } = await import('../src/sidecar/graph/index.js')
        const model = loadRun(runPath)
        return { index: new GraphIndex(model), kuzu: {} as never, oxigraph: {} as never, result: {} as never }
      },
      stream: () => {},
      runsRoot: '',
      repoRoot: join(GOLDEN_RUN, '..', '..', '..')
    })) as { questions: QAPair[]; expected_labels: Record<string, string> }
    expect(result.questions.length).toBeGreaterThan(0)
    const withIds = result.questions.find((q) => q.expected_ids.length > 0)!
    expect(result.expected_labels[withIds.expected_ids[0]]).toBeTruthy()
  })

  it('evalsSample (readQuestions + sample.ts) picks a deterministic subset of the golden question set', async () => {
    const handler = getOp('evalsSample')
    expect(handler).toBeDefined()
    const call = () =>
      handler!({
        requestId: 'r2',
        params: { runPath: GOLDEN_RUN, fraction: 0.1, seed: 3, stratify: true },
        ensureLoaded: async () => {
          throw new Error('not needed')
        },
        stream: () => {},
        runsRoot: '',
        repoRoot: ''
      }) as Promise<{ ids: string[]; count: number }>
    const a = await call()
    const b = await call()
    expect(a.ids).toEqual(b.ids) // deterministic for the same seed
    expect(a.count).toBeGreaterThan(0)
  })
})
