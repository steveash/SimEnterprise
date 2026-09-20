// The evals-only MCP tool wiring (submit_answer, propose_question) and the
// propose-chat's validation, driven without the SDK — like tests/agent.test.ts
// (EXPLORER_EVALS.md §6).
import { describe, it, expect } from 'vitest'
import { GraphIndex } from '../src/sidecar/graph/index.js'
import {
  buildSubmitAnswerServer,
  buildSubmitAnswerTool,
  SUBMIT_ANSWER_TOOL,
  buildProposeQuestionServer,
  buildProposeQuestionTool,
  PROPOSE_QUESTION_TOOL
} from '../src/sidecar/evals/tools.js'
import { validateProposal } from '../src/sidecar/evals/propose.js'
import type { GraphModel } from '../src/shared/model.js'

function tinyModel(): GraphModel {
  const now = '2026-01-01T00:00:00Z'
  return {
    runId: 'test-run',
    runPath: '/nonexistent/test-run',
    nodes: [
      { id: 'person:ash', type: 'Person', label: 'Steve Ash', aliases: ['Ash'], created_at: now, props: {} },
      { id: 'team:nitro', type: 'Team', label: 'Nitro', aliases: [], created_at: now, props: {} }
    ],
    edges: [{ id: 'e:member', type: 'member_of', src: 'person:ash', dst: 'team:nitro', created_at: now, props: {} }],
    mentions: [],
    events: [],
    provenance: [],
    aliases: [],
    validation: [],
    manifest: null,
    nodeTypes: ['Person', 'Team'],
    edgeTypes: ['member_of'],
    timeRange: null
  }
}

describe('submit_answer MCP server', () => {
  it('is an sdk server exposing exactly one tool, prefixed correctly', () => {
    const server = buildSubmitAnswerServer({ onSubmit: () => {} })
    expect(server.type).toBe('sdk')
    expect(SUBMIT_ANSWER_TOOL).toBe('mcp__enterprise-sim-evals__submit_answer')
  })

  it('records the submitted node ids + label via onSubmit', async () => {
    const calls: { node_ids: string[]; label?: string }[] = []
    const t = buildSubmitAnswerTool({ onSubmit: (a) => calls.push(a) })
    const result = await (t.handler as (args: unknown) => Promise<{ content: { text: string }[] }>)({
      node_ids: ['person:ash', 'team:nitro'],
      label: '2'
    })
    expect(result.content[0].text).toContain('2 node id(s)')
    expect(calls).toEqual([{ node_ids: ['person:ash', 'team:nitro'], label: '2' }])
  })
})

describe('propose_question MCP server', () => {
  it('is an sdk server exposing exactly one tool, prefixed correctly', () => {
    const server = buildProposeQuestionServer({ validate: () => ({ valid: true, normalized: emptyProposal() }), onProposal: () => {} })
    expect(server.type).toBe('sdk')
    expect(PROPOSE_QUESTION_TOOL).toBe('mcp__enterprise-sim-evals__propose_question')
  })

  it('reports validation failures as an isError tool result and still calls onProposal', async () => {
    const seen: { proposal: ReturnType<typeof emptyProposal>; valid: boolean; reason?: string }[] = []
    const t = buildProposeQuestionTool({
      validate: () => ({ valid: false, reason: 'unknown node id(s): person:nobody', normalized: emptyProposal() }),
      onProposal: (e) => seen.push(e)
    })
    const result = await (t.handler as (args: unknown) => Promise<{ content: { text: string }[]; isError?: boolean }>)({
      question: 'x',
      reasoning_type: 'direct_relation',
      expected_ids: ['person:nobody']
    })
    expect(result.isError).toBe(true)
    expect(result.content[0].text).toContain('rejected')
    expect(seen).toEqual([{ proposal: emptyProposal(), valid: false, reason: 'unknown node id(s): person:nobody' }])
  })

  it('reports acceptance as a plain tool result and calls onProposal with valid:true', async () => {
    const seen: { proposal: ReturnType<typeof emptyProposal>; valid: boolean; reason?: string }[] = []
    const t = buildProposeQuestionTool({
      validate: () => ({ valid: true, normalized: emptyProposal() }),
      onProposal: (e) => seen.push(e)
    })
    const result = await (t.handler as (args: unknown) => Promise<{ content: { text: string }[]; isError?: boolean }>)({
      question: 'Who does Ash report to?',
      reasoning_type: 'direct_relation',
      expected_ids: ['person:ash']
    })
    expect(result.isError).toBeUndefined()
    expect(seen).toEqual([{ proposal: emptyProposal(), valid: true, reason: undefined }])
  })
})

function emptyProposal() {
  return { question: '', reasoning_type: '', expected_ids: [] as string[], difficulty: 'medium', tags: [] as string[] }
}

describe('validateProposal (propose.ts) — pure validation used by the sidecar', () => {
  const index = new GraphIndex(tinyModel())

  it('accepts a well-formed proposal grounded in real node ids', () => {
    const r = validateProposal({ index }, new Set(), {
      question: 'Who does Steve Ash report to?',
      reasoning_type: 'direct_relation',
      expected_ids: ['person:ash'],
      difficulty: 'easy'
    })
    expect(r.valid).toBe(true)
  })

  it('rejects an empty question', () => {
    const r = validateProposal({ index }, new Set(), { reasoning_type: 'direct_relation', expected_ids: ['person:ash'] })
    expect(r.valid).toBe(false)
    expect(r.reason).toMatch(/empty question/)
  })

  it('rejects an invalid reasoning_type', () => {
    const r = validateProposal({ index }, new Set(), {
      question: 'x?',
      reasoning_type: 'not_a_real_type',
      expected_ids: ['person:ash']
    })
    expect(r.valid).toBe(false)
    expect(r.reason).toMatch(/reasoning_type/)
  })

  it('rejects an expected_ids entry that is not a real node id', () => {
    const r = validateProposal({ index }, new Set(), {
      question: 'x?',
      reasoning_type: 'direct_relation',
      expected_ids: ['person:nobody']
    })
    expect(r.valid).toBe(false)
    expect(r.reason).toMatch(/unknown node id/)
  })

  it('rejects an empty expected_ids', () => {
    const r = validateProposal({ index }, new Set(), { question: 'x?', reasoning_type: 'direct_relation', expected_ids: [] })
    expect(r.valid).toBe(false)
    expect(r.reason).toMatch(/expected_ids is empty/)
  })

  it('rejects a duplicate of an existing question by normalized text', () => {
    const seen = new Set(['who does steve ash report to?'])
    const r = validateProposal({ index }, seen, {
      question: '  Who   does Steve Ash report to?  ',
      reasoning_type: 'direct_relation',
      expected_ids: ['person:ash']
    })
    expect(r.valid).toBe(false)
    expect(r.reason).toMatch(/duplicate/)
  })
})
