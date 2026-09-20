// The eval-proposal chat (EXPLORER_EVALS.md §4): "I want questions like X" turned
// into grounded QAPairs via `propose_question`, validated as each call arrives.
// Like answerer.ts, this drives the Agent SDK loop directly (a second MCP server
// alongside the reused graph tools) rather than editing agent/harness.ts.
import { query } from '@anthropic-ai/claude-agent-sdk'
import { buildMcpServer, TOOL_NAMES } from '../agent/tools.js'
import { finalQueryFromEvents, type AgentEvent, type ChatEngines } from '../agent/harness.js'
import { REASONING_TYPES, type ProposeStreamEvent, type ProposedQuestion } from '../../shared/evals-protocol.js'
import { buildProposeQuestionServer, PROPOSE_QUESTION_TOOL, type ProposalValidation, type RawProposal } from './tools.js'
import { referenceExamplesBlock } from './reference.js'
import { readQuestions } from './store.js'

function normalize(text: string): string {
  return text.trim().toLowerCase().replace(/\s+/g, ' ')
}

/**
 * Validate one raw `propose_question` call: ids must exist in the loaded model,
 * reasoning_type must be valid, and the question must not duplicate an existing
 * one (by normalized text) — either already in the run's set or accepted earlier
 * in this same conversation (`seenNormalized`, mutated by the caller on accept).
 */
export function validateProposal(
  engines: Pick<ChatEngines, 'index'>,
  seenNormalized: ReadonlySet<string>,
  raw: RawProposal
): ProposalValidation {
  const question = typeof raw.question === 'string' ? raw.question.trim() : ''
  const reasoning_type = typeof raw.reasoning_type === 'string' ? raw.reasoning_type : ''
  const expected_ids = Array.isArray(raw.expected_ids) ? raw.expected_ids.map(String) : []
  const expected_label = raw.expected_label != null ? String(raw.expected_label) : undefined
  const difficulty = typeof raw.difficulty === 'string' && raw.difficulty ? raw.difficulty : 'medium'
  const rationale = typeof raw.rationale === 'string' ? raw.rationale : undefined
  const tags = Array.isArray(raw.tags) ? raw.tags.map(String) : []
  const normalized: ProposedQuestion = { question, reasoning_type, expected_ids, expected_label, difficulty, rationale, tags }

  if (!question) return { valid: false, reason: 'empty question', normalized }
  if (!(REASONING_TYPES as readonly string[]).includes(reasoning_type)) {
    return { valid: false, reason: `reasoning_type must be one of ${REASONING_TYPES.join(', ')}`, normalized }
  }
  if (expected_ids.length === 0) return { valid: false, reason: 'expected_ids is empty', normalized }
  const unknown = expected_ids.filter((id) => !engines.index.getNode(id))
  if (unknown.length) return { valid: false, reason: `unknown node id(s): ${unknown.join(', ')}`, normalized }
  if (seenNormalized.has(normalize(question))) {
    return { valid: false, reason: 'duplicate of an existing question', normalized }
  }
  return { valid: true, normalized }
}

function buildSystemPrompt(e: ChatEngines): string {
  return `You help curate the KG-QA eval question set for an Enterprise-Sim gold knowledge graph. You have
the same graph tools as the explorer's chat (cypher_query, sparql_query, search_nodes, neighbors, …)
plus propose_question.

RULES (strict):
- Every proposal's expected_ids MUST be derived by actually RUNNING a query (cypher_query or
  sparql_query, or search_nodes/neighbors for simple lookups) against the graph just now — never
  recalled from memory or guessed. Resolve names to ids with search_nodes first.
- Propose 3-8 varied questions per turn. Mix reasoning types unless the user asked for a specific one;
  favour whichever reasoning type(s) the user asked for.
- Call propose_question once per question, with expected_ids as the exact sorted set your query
  returned, a valid reasoning_type, and a one-sentence rationale citing how you verified the answer.
- If propose_question rejects a proposal, read why and either fix it (in this same turn) or drop it.

Worked patterns per reasoning type (substitute the subject node id for <id>):
${referenceExamplesBlock()}

CURRENT GRAPH:
  node types: ${e.index.model.nodeTypes.join(', ')}
  edge types: ${e.index.model.edgeTypes.join(', ')}
  ${e.index.model.nodes.length} nodes, ${e.index.model.edges.length} edges, ${e.oxigraph.inferredCount} inferred triples.`
}

function previewToolResult(content: unknown): string {
  let text = ''
  if (typeof content === 'string') text = content
  else if (Array.isArray(content)) {
    text = content
      .map((c) => (typeof c === 'object' && c && 'text' in c ? String((c as { text: unknown }).text) : ''))
      .join('')
  } else text = JSON.stringify(content)
  return text.length > 400 ? text.slice(0, 400) + '…' : text
}

/** Run one proposal-chat turn. Streams AgentEvents plus `{kind:'proposal', …}` via `onEvent`. */
export async function runPropose(
  engines: ChatEngines,
  opts: {
    runPath: string
    prompt: string
    model?: string
    resume?: string | null
    signal?: AbortSignal
    onEvent: (e: ProposeStreamEvent) => void
  }
): Promise<void> {
  const seen = new Set(readQuestions(opts.runPath).map((q) => normalize(q.question)))

  const graphServer = buildMcpServer({
    index: engines.index,
    kuzu: engines.kuzu,
    oxigraph: engines.oxigraph,
    emitViz: () => {} // the propose chat never touches the shared graph view
  })
  const proposeServer = buildProposeQuestionServer({
    validate: (raw) => validateProposal(engines, seen, raw),
    onProposal: (evt) => {
      if (evt.valid) seen.add(normalize(evt.proposal.question))
      opts.onEvent({ kind: 'proposal', ...evt })
    }
  })

  let sessionId: string | null = opts.resume ?? null
  const turnEvents: AgentEvent[] = []
  const emit = (e: AgentEvent) => {
    turnEvents.push(e)
    opts.onEvent(e)
  }

  const q = query({
    prompt: opts.prompt,
    options: {
      model: opts.model ?? 'sonnet',
      systemPrompt: buildSystemPrompt(engines),
      mcpServers: { 'enterprise-sim-graph': graphServer, 'enterprise-sim-evals': proposeServer },
      allowedTools: [...TOOL_NAMES, PROPOSE_QUESTION_TOOL],
      disallowedTools: ['Bash', 'Read', 'Write', 'Edit', 'WebFetch', 'WebSearch', 'Glob', 'Grep'],
      permissionMode: 'bypassPermissions',
      maxTurns: 20,
      ...(opts.resume ? { resume: opts.resume } : {})
    }
  })

  if (opts.signal) {
    opts.signal.addEventListener('abort', () => {
      void q.interrupt().catch(() => {})
    })
  }

  try {
    for await (const msg of q) {
      if (opts.signal?.aborted) break
      switch (msg.type) {
        case 'system':
          if ('session_id' in msg && msg.session_id) sessionId = msg.session_id
          break
        case 'assistant': {
          for (const block of msg.message.content) {
            if (block.type === 'text' && block.text) {
              emit({ kind: 'text', text: block.text })
            } else if (block.type === 'thinking' && 'thinking' in block && block.thinking) {
              emit({ kind: 'thinking', text: String(block.thinking) })
            } else if (block.type === 'tool_use') {
              emit({
                kind: 'tool_use',
                id: block.id,
                name: block.name.replace(/^mcp__(enterprise-sim-graph|enterprise-sim-evals)__/, ''),
                input: block.input
              })
            }
          }
          break
        }
        case 'user': {
          const content = msg.message.content
          if (Array.isArray(content)) {
            for (const block of content) {
              if (typeof block === 'object' && block && 'type' in block && block.type === 'tool_result') {
                const tr = block as { tool_use_id: string; is_error?: boolean; content: unknown }
                emit({ kind: 'tool_result', id: tr.tool_use_id, ok: !tr.is_error, preview: previewToolResult(tr.content) })
              }
            }
          }
          break
        }
        case 'result': {
          if ('session_id' in msg && msg.session_id) sessionId = msg.session_id
          const fq = finalQueryFromEvents(turnEvents)
          if (fq) emit({ kind: 'final_query', engine: fq.engine, query: fq.query })
          emit({ kind: 'done', sessionId })
          return
        }
        default:
          break
      }
    }
    emit({ kind: 'done', sessionId })
  } catch (e) {
    emit({ kind: 'error', message: (e as Error).message })
    emit({ kind: 'done', sessionId })
  }
}
