// The explorer's in-process eval answerer (EXPLORER_EVALS.md §3). One question =
// one Agent SDK turn with the graph tools plus `submit_answer`; system prompt
// built from the benchmark's reasoning-type reference queries (reference.ts,
// ported from benchmark/runners/reference.py) and the strict "ids only, call
// submit_answer exactly once" instruction. In batch mode `emitViz` is a no-op
// (no side effects on the shared graph view); a single-question run wires it
// through so the caller can highlight predicted vs expected.
//
// This duplicates (rather than imports) the Agent SDK message-driving loop in
// agent/harness.ts's `runChat`, because this turn needs a different tool set
// (graph tools + submit_answer, not the general chat toolset) and must not
// touch that file — see docs/EXPLORER.md's instruction to extend the harness
// from this module. `buildMcpServer`, `TOOL_NAMES` and `finalQueryFromEvents`
// ARE reused unmodified.
import { query } from '@anthropic-ai/claude-agent-sdk'
import { buildMcpServer, TOOL_NAMES } from '../agent/tools.js'
import { finalQueryFromEvents, type AgentEvent, type ChatEngines } from '../agent/harness.js'
import type { VizEvent } from '../../shared/agent-events.js'
import { buildSubmitAnswerServer, SUBMIT_ANSWER_TOOL, type SubmittedAnswer } from './tools.js'
import { referenceExamplesBlock } from './reference.js'

export const DEFAULT_MAX_TURNS = 24

export interface AnswerResult {
  predicted_ids: string[]
  label?: string
  turns: number
  engine?: 'Cypher' | 'SPARQL'
  query?: string
  seconds: number
  cost_usd?: number
  usage?: unknown
  error?: string
  /** The turn's full event stream — used for the single-question trace + final-query display. */
  events: AgentEvent[]
}

function buildSystemPrompt(e: ChatEngines): string {
  return `You are answering ONE question from a knowledge-graph QA benchmark over an Enterprise-Sim
"gold knowledge graph" — a synthetic but internally-consistent snapshot of a company (people, teams,
departments, goals, initiatives, projects, and the document artifacts that ground them).

You have two query engines:

• cypher_query — Cypher (property-graph / Kùzu). Prefer it for traversal and variable-length paths
  (\`-[:reports_to*1..]->\`; write \`*1..\`, NOT \`*0..\` — Kùzu rejects a zero lower bound).
• sparql_query — SPARQL (RDF / Oxigraph) with a reasoning layer. An ontology has materialized
  INFERRED predicates not in the raw data: der:manages, der:manages_chain, der:reports_to_chain,
  der:in_department, der:advances_goal_effective, der:subgoal_of_chain, der:subinitiative_of_chain.
  Prefer it whenever the question needs one of these derived/transitive facts.

Worked patterns per reasoning type (substitute the subject node id for <id>):
${referenceExamplesBlock()}

WORKFLOW:
1. Resolve any human names/phrases to node ids with search_nodes before querying (call graph_schema
   first if you are unsure of labels/columns/predicates).
2. Run the query that answers the question. A count/aggregation question's answer is the FULL SET
   being counted, not just its size.
3. Call submit_answer EXACTLY ONCE with the final list of node ids — ids only, no names, no prose.
   Submit an empty list only if nothing in the graph answers the question.

CURRENT GRAPH:
  node types: ${e.index.model.nodeTypes.join(', ')}
  edge types: ${e.index.model.edgeTypes.join(', ')}
  ${e.index.model.nodes.length} nodes, ${e.index.model.edges.length} edges, ${e.oxigraph.inferredCount} inferred triples.`
}

function extractCost(msg: unknown): { cost_usd?: number; usage?: unknown } {
  if (!msg || typeof msg !== 'object') return {}
  const m = msg as Record<string, unknown>
  return {
    cost_usd: typeof m.total_cost_usd === 'number' ? m.total_cost_usd : undefined,
    usage: 'usage' in m ? m.usage : undefined
  }
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

/** Run one question through the graph agent; returns its prediction + trace. */
export async function answerQuestion(
  engines: ChatEngines,
  opts: {
    question: string
    model?: string
    maxTurns?: number
    signal?: AbortSignal
    /** Defaults to a no-op — batch runs must not touch the shared graph view. */
    emitViz?: (v: VizEvent) => void
  }
): Promise<AnswerResult> {
  const events: AgentEvent[] = []
  let submitted: SubmittedAnswer | null = null
  let turns = 0

  const graphServer = buildMcpServer({
    index: engines.index,
    kuzu: engines.kuzu,
    oxigraph: engines.oxigraph,
    emitViz: opts.emitViz ?? (() => {})
  })
  const evalsServer = buildSubmitAnswerServer({
    onSubmit: (a) => {
      submitted = a
    }
  })

  const start = Date.now()
  let costInfo: { cost_usd?: number; usage?: unknown } = {}

  const q = query({
    prompt:
      `Question: ${opts.question}\n\n` +
      'Use the graph tools to determine the answer, then call submit_answer exactly once with the ' +
      'final list of node ids. The answer is a SET of node ids.',
    options: {
      model: opts.model ?? 'sonnet',
      systemPrompt: buildSystemPrompt(engines),
      mcpServers: { 'enterprise-sim-graph': graphServer, 'enterprise-sim-evals': evalsServer },
      allowedTools: [...TOOL_NAMES, SUBMIT_ANSWER_TOOL],
      disallowedTools: ['Bash', 'Read', 'Write', 'Edit', 'WebFetch', 'WebSearch', 'Glob', 'Grep'],
      permissionMode: 'bypassPermissions',
      maxTurns: opts.maxTurns ?? DEFAULT_MAX_TURNS
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
        case 'assistant': {
          turns++
          for (const block of msg.message.content) {
            if (block.type === 'text' && block.text) {
              events.push({ kind: 'text', text: block.text })
            } else if (block.type === 'thinking' && 'thinking' in block && block.thinking) {
              events.push({ kind: 'thinking', text: String(block.thinking) })
            } else if (block.type === 'tool_use') {
              events.push({
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
                events.push({ kind: 'tool_result', id: tr.tool_use_id, ok: !tr.is_error, preview: previewToolResult(tr.content) })
              }
            }
          }
          break
        }
        case 'result': {
          costInfo = extractCost(msg)
          break
        }
        default:
          break
      }
    }
  } catch (e) {
    const seconds = (Date.now() - start) / 1000
    return { predicted_ids: submitted ? (submitted as SubmittedAnswer).node_ids : [], turns, seconds, error: (e as Error).message, events }
  }

  const fq = finalQueryFromEvents(events)
  const seconds = (Date.now() - start) / 1000
  const answer = submitted as SubmittedAnswer | null
  return {
    predicted_ids: answer?.node_ids ?? [],
    label: answer?.label,
    turns,
    engine: fq?.engine,
    query: fq?.query,
    seconds,
    ...costInfo,
    events
  }
}
