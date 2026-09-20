// The evals-only MCP tools: `submit_answer` (EXPLORER_EVALS.md §3) and
// `propose_question` (§4). Built as a SEPARATE MCP server from
// agent/tools.ts's graph server (which we reuse unmodified via `buildMcpServer`)
// so both can be passed together in one turn's `mcpServers`, per docs/EXPLORER.md's
// instruction to extend the harness without editing it.
import { z } from 'zod'
import { tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk'
import { REASONING_TYPES, type ProposedQuestion } from '../../shared/evals-protocol.js'

const SERVER_NAME = 'enterprise-sim-evals'

function ok(data: unknown) {
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2)
  return { content: [{ type: 'text' as const, text }] }
}

// --------------------------------------------------------------------------
// submit_answer — one question = one call. The answerer reads the last call's
// (or only call's) node_ids/label as the turn's prediction.
// --------------------------------------------------------------------------

export interface SubmittedAnswer {
  node_ids: string[]
  label?: string
}

export interface SubmitAnswerContext {
  onSubmit: (answer: SubmittedAnswer) => void
}

/** The raw tool definition, exposed separately so tests can invoke its handler directly (mirrors agent/tools.ts's `buildTools`). */
export function buildSubmitAnswerTool(ctx: SubmitAnswerContext) {
  return tool(
    'submit_answer',
    'Submit the final answer to the question: the set of knowledge-graph node ids that answer it. ' +
      'Call this exactly once, after you have determined the answer with the graph tools. Answer with ' +
      'ids only (no names, no prose) — an empty list if nothing in the graph answers the question.',
    {
      node_ids: z
        .array(z.string())
        .describe('the KG node ids that answer the question (a set; order does not matter)'),
      label: z.string().optional().describe('optional human-readable label for the answer, e.g. a count')
    },
    async (args) => {
      ctx.onSubmit({ node_ids: args.node_ids, label: args.label })
      return ok(`recorded ${args.node_ids.length} node id(s)`)
    }
  )
}

export function buildSubmitAnswerServer(ctx: SubmitAnswerContext) {
  return createSdkMcpServer({ name: SERVER_NAME, version: '0.1.0', tools: [buildSubmitAnswerTool(ctx)] })
}

export const SUBMIT_ANSWER_TOOL = `mcp__${SERVER_NAME}__submit_answer`

// --------------------------------------------------------------------------
// propose_question — the proposal chat's tool (EXPLORER_EVALS.md §4). The
// sidecar validates every call as it arrives (ids exist in the loaded model,
// reasoning_type is valid, not a duplicate by normalized question text) and
// reports the outcome back to the agent as the tool result, so a rejected
// proposal can be corrected within the same turn.
// --------------------------------------------------------------------------

export interface RawProposal {
  question?: unknown
  reasoning_type?: unknown
  expected_ids?: unknown
  expected_label?: unknown
  difficulty?: unknown
  rationale?: unknown
  tags?: unknown
}

export interface ProposalValidation {
  valid: boolean
  reason?: string
  normalized: ProposedQuestion
}

export interface ProposeQuestionContext {
  validate: (raw: RawProposal) => ProposalValidation
  onProposal: (evt: { proposal: ProposedQuestion; valid: boolean; reason?: string }) => void
}

/** The raw tool definition, exposed separately so tests can invoke its handler directly (mirrors agent/tools.ts's `buildTools`). */
export function buildProposeQuestionTool(ctx: ProposeQuestionContext) {
  return tool(
    'propose_question',
    'Propose one new eval question for the run\'s KG-QA question set. Every answer MUST have been derived ' +
      'from an actual query you just ran (cypher_query/sparql_query/search_nodes/neighbors) — never recalled ' +
      'from memory. expected_ids must be the exact set of node ids your query returned.',
    {
      question: z.string().describe('the natural-language question'),
      reasoning_type: z.enum(REASONING_TYPES).describe('the kind of reasoning the question exercises'),
      expected_ids: z.array(z.string()).min(1).describe('the gold answer: KG node ids, derived from a query'),
      expected_label: z.string().optional().describe('a human-readable rendering of the answer, e.g. a count'),
      difficulty: z.enum(['easy', 'medium', 'hard']).optional(),
      rationale: z.string().optional().describe('one sentence: why this question, and how you verified the answer'),
      tags: z.array(z.string()).optional()
    },
    async (args) => {
      const { valid, reason, normalized } = ctx.validate(args)
      ctx.onProposal({ proposal: normalized, valid, reason })
      return valid
        ? ok('accepted for review')
        : { content: [{ type: 'text' as const, text: `rejected: ${reason}` }], isError: true }
    }
  )
}

export function buildProposeQuestionServer(ctx: ProposeQuestionContext) {
  return createSdkMcpServer({ name: SERVER_NAME, version: '0.1.0', tools: [buildProposeQuestionTool(ctx)] })
}

export const PROPOSE_QUESTION_TOOL = `mcp__${SERVER_NAME}__propose_question`
