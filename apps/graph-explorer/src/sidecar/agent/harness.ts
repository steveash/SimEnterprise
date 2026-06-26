import { query } from '@anthropic-ai/claude-agent-sdk'
import type { GraphIndex } from '../graph/index.js'
import type { KuzuEngine } from '../graph/kuzu.js'
import type { OxigraphEngine } from '../graph/rdf.js'
import { buildMcpServer, TOOL_NAMES } from './tools.js'
import type { AgentEvent } from '../../shared/agent-events.js'

export type { AgentEvent }

export interface ChatEngines {
  index: GraphIndex
  kuzu: KuzuEngine
  oxigraph: OxigraphEngine
}

function buildSystemPrompt(e: ChatEngines): string {
  return `You are the reasoning agent inside an interactive explorer for an Enterprise-Sim
"gold knowledge graph" — a synthetic but internally-consistent snapshot of a company
(people, teams, departments, goals, initiatives, projects, calendar events, and the
document artifacts that ground them).

You answer questions about THIS graph by calling tools. You have two query engines:

• cypher_query  — a Cypher (property-graph / Kùzu) engine. Prefer it for traversal,
  variable-length paths (e.g. \`-[:reports_to*1..]->\`), and filtering on node/edge
  property columns.
• sparql_query  — a SPARQL (RDF / Oxigraph) engine WITH a reasoning layer. An ontology
  has materialized *inferred* predicates that are NOT in the raw data. Prefer it when a
  question needs entailment/derived facts:
    der:manages, der:manages_chain, der:reports_to_chain (transitive management),
    der:in_department (person→department via team membership or leadership),
    der:advances_goal_effective (advances a goal OR any of its parent goals),
    der:subgoal_of_chain, der:subinitiative_of_chain.

WORKFLOW:
1. Call graph_schema once if you are unsure of labels, columns, or predicates.
2. Resolve any human names/phrases to node ids with search_nodes before querying.
3. Run the query. Pick the engine that fits — say which one you used and why.
4. When you have the answer, present it concisely. The tools you call already
   highlight results on the graph; you may also call highlight_nodes to spotlight the
   precise answer set and focus the view.
5. Cite with provenance/read_artifact when the user asks "where did this come from".

Be concise and concrete. Show the query you ran. Node ids look like \`person:ben-cho\`,
\`goal:2\`, \`initiative:engineering-build-software\`.

CURRENT GRAPH:
  node types: ${e.index.model.nodeTypes.join(', ')}
  edge types: ${e.index.model.edgeTypes.join(', ')}
  ${e.index.model.nodes.length} nodes, ${e.index.model.edges.length} edges, ${e.oxigraph.inferredCount} inferred triples.`
}

/**
 * Run one chat turn. Streams AgentEvents via `onEvent`. Returns the session id so
 * the caller can pass it as `resume` on the next turn for multi-turn memory.
 */
export async function runChat(
  engines: ChatEngines,
  opts: {
    prompt: string
    model?: string
    resume?: string | null
    signal?: AbortSignal
    onEvent: (e: AgentEvent) => void
  }
): Promise<void> {
  const mcpServer = buildMcpServer({
    index: engines.index,
    kuzu: engines.kuzu,
    oxigraph: engines.oxigraph,
    emitViz: (viz) => opts.onEvent({ kind: 'viz', viz })
  })

  let sessionId: string | null = opts.resume ?? null

  const q = query({
    prompt: opts.prompt,
    options: {
      model: opts.model ?? 'sonnet',
      systemPrompt: buildSystemPrompt(engines),
      mcpServers: { 'enterprise-sim-graph': mcpServer },
      allowedTools: TOOL_NAMES,
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
              opts.onEvent({ kind: 'text', text: block.text })
            } else if (block.type === 'thinking' && 'thinking' in block && block.thinking) {
              opts.onEvent({ kind: 'thinking', text: String(block.thinking) })
            } else if (block.type === 'tool_use') {
              opts.onEvent({
                kind: 'tool_use',
                id: block.id,
                name: block.name.replace(/^mcp__enterprise-sim-graph__/, ''),
                input: block.input
              })
            }
          }
          break
        }
        case 'user': {
          // tool_result frames arrive as user messages
          const content = msg.message.content
          if (Array.isArray(content)) {
            for (const block of content) {
              if (typeof block === 'object' && block && 'type' in block && block.type === 'tool_result') {
                const tr = block as { tool_use_id: string; is_error?: boolean; content: unknown }
                opts.onEvent({
                  kind: 'tool_result',
                  id: tr.tool_use_id,
                  ok: !tr.is_error,
                  preview: previewToolResult(tr.content)
                })
              }
            }
          }
          break
        }
        case 'result': {
          if ('session_id' in msg && msg.session_id) sessionId = msg.session_id
          const usage = 'usage' in msg ? msg.usage : undefined
          opts.onEvent({ kind: 'done', sessionId, usage })
          return
        }
      }
    }
    opts.onEvent({ kind: 'done', sessionId })
  } catch (e) {
    opts.onEvent({ kind: 'error', message: (e as Error).message })
    opts.onEvent({ kind: 'done', sessionId })
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
