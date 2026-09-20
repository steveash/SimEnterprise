// Live agent tests for the evals answerer/propose chat, gated on
// ANTHROPIC_API_KEY (and the golden run) exactly like tests/agent.test.ts's
// gated live turn (EXPLORER_EVALS.md §6). Skipped, not failed, on a checkout
// without a key or without runs/golden/.
import { describe, it, expect } from 'vitest'
import { GraphIndex } from '../src/sidecar/graph/index.js'
import { KuzuEngine } from '../src/sidecar/graph/kuzu.js'
import { OxigraphEngine } from '../src/sidecar/graph/rdf.js'
import { answerQuestion } from '../src/sidecar/evals/answerer.js'
import { runPropose } from '../src/sidecar/evals/propose.js'
import type { ProposeStreamEvent } from '../src/shared/evals-protocol.js'
import { loadGolden, goldenRunExists } from './helpers.js'

const liveEnabled = !!process.env.ANTHROPIC_API_KEY && goldenRunExists()

describe.skipIf(!liveEnabled)('live evals answerer (gated on ANTHROPIC_API_KEY)', () => {
  it(
    'answers a direct_relation question by calling submit_answer exactly once',
    async () => {
      const model = loadGolden()
      const index = new GraphIndex(model)
      const kuzu = await KuzuEngine.build(model)
      const oxigraph = OxigraphEngine.build(model)

      const res = await answerQuestion(
        { index, kuzu, oxigraph },
        { question: 'Who does the Senior Engineer report to?', model: 'haiku', maxTurns: 24 }
      )

      expect(res.error).toBeUndefined()
      expect(Array.isArray(res.predicted_ids)).toBe(true)
      const submitCalls = res.events.filter((e) => e.kind === 'tool_use' && e.name === 'submit_answer')
      expect(submitCalls.length).toBeGreaterThanOrEqual(1)
    },
    60_000
  )
})

describe.skipIf(!liveEnabled)('live propose chat (gated on ANTHROPIC_API_KEY)', () => {
  it(
    'proposes at least one grounded, valid question',
    async () => {
      const model = loadGolden()
      const index = new GraphIndex(model)
      const kuzu = await KuzuEngine.build(model)
      const oxigraph = OxigraphEngine.build(model)

      const events: ProposeStreamEvent[] = []
      await runPropose(
        { index, kuzu, oxigraph },
        {
          runPath: model.runPath,
          prompt: 'Propose 2 direct_relation questions about who reports to whom.',
          model: 'haiku',
          onEvent: (e) => events.push(e)
        }
      )

      const proposals = events.filter((e): e is Extract<ProposeStreamEvent, { kind: 'proposal' }> => e.kind === 'proposal')
      expect(proposals.length).toBeGreaterThanOrEqual(1)
      expect(proposals.some((p) => p.valid)).toBe(true)
      expect(events.some((e) => e.kind === 'done')).toBe(true)
    },
    60_000
  )
})
