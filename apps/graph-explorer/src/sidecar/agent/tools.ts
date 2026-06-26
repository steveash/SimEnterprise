import { z } from 'zod'
import { tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk'
import type { GraphIndex } from '../graph/index.js'
import type { KuzuEngine } from '../graph/kuzu.js'
import type { OxigraphEngine } from '../graph/rdf.js'

import type { VizEvent } from '../../shared/agent-events.js'
export type { VizEvent }

export interface ToolContext {
  index: GraphIndex
  kuzu: KuzuEngine
  oxigraph: OxigraphEngine
  emitViz: (e: VizEvent) => void
}

function ok(data: unknown) {
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2)
  return { content: [{ type: 'text' as const, text }] }
}

function err(message: string) {
  return { content: [{ type: 'text' as const, text: `ERROR: ${message}` }], isError: true }
}

/** Pull any values from query rows that are known node ids, for auto-highlight. */
function nodeIdsInRows(ctx: ToolContext, rows: Record<string, unknown>[]): string[] {
  const ids = new Set<string>()
  for (const row of rows) {
    for (const v of Object.values(row)) {
      if (typeof v === 'string' && ctx.index.getNode(v)) ids.add(v)
    }
  }
  return [...ids]
}

export function buildMcpServer(ctx: ToolContext) {
  const search = tool(
    'search_nodes',
    'Full-text search the graph for nodes by name/label/alias/property. Returns ranked hits with their ids and types. Use this first to resolve a human name or phrase to a node id.',
    {
      query: z.string().describe('text to search for, e.g. a person name or initiative title'),
      types: z.array(z.string()).optional().describe('restrict to these node types'),
      limit: z.number().optional()
    },
    async (args) => {
      const hits = ctx.index.search(args.query, { types: args.types, limit: args.limit })
      if (hits.length) ctx.emitViz({ type: 'highlight', nodeIds: hits.map((h) => h.id), note: `search: ${args.query}` })
      return ok(hits)
    }
  )

  const details = tool(
    'node_details',
    'Get the full properties, type, aliases and label for a single node id.',
    { id: z.string() },
    async (args) => {
      const n = ctx.index.getNode(args.id)
      if (!n) return err(`no node with id ${args.id}`)
      ctx.emitViz({ type: 'focus', nodeIds: [n.id] })
      return ok(n)
    }
  )

  const neighbors = tool(
    'neighbors',
    'Return the subgraph within N hops of a node (ids of nodes + connecting edges). Highlights it on the graph. Good for "who/what is connected to X".',
    {
      id: z.string(),
      depth: z.number().optional().describe('hops, 1-6 (default 1)'),
      edgeTypes: z.array(z.string()).optional().describe('only traverse these edge types'),
      direction: z.enum(['out', 'in', 'both']).optional()
    },
    async (args) => {
      if (!ctx.index.getNode(args.id)) return err(`no node with id ${args.id}`)
      const sub = ctx.index.neighbors(args.id, {
        depth: args.depth,
        edgeTypes: args.edgeTypes,
        direction: args.direction
      })
      ctx.emitViz({ type: 'highlight', nodeIds: sub.nodeIds, edgeIds: sub.edgeIds, note: `neighbors of ${args.id}` })
      const labeled = sub.nodeIds.map((id) => {
        const n = ctx.index.getNode(id)!
        return { id, type: n.type, label: n.label }
      })
      return ok({ count: labeled.length, nodes: labeled, edgeIds: sub.edgeIds })
    }
  )

  const path = tool(
    'shortest_path',
    'Find a shortest path between two node ids and highlight it. Answers "how is X connected to Y".',
    { from: z.string(), to: z.string(), directed: z.boolean().optional() },
    async (args) => {
      const p = ctx.index.shortestPath(args.from, args.to, { directed: args.directed })
      if (!p) return ok({ found: false })
      ctx.emitViz({ type: 'highlight', nodeIds: p.nodeIds, edgeIds: p.edgeIds, note: 'shortest path' })
      const steps = p.nodeIds.map((id) => ({ id, label: ctx.index.getNode(id)?.label }))
      return ok({ found: true, hops: p.edgeIds.length, path: steps })
    }
  )

  const cypher = tool(
    'cypher_query',
    'Run a Cypher query against the property graph (Kùzu engine). Best for traversal, variable-length paths, and filtering on node/edge properties. Returns rows. Node ids in the result are auto-highlighted. Call graph_schema first if unsure of labels/columns.',
    { query: z.string().describe('a Cypher query') },
    async (args) => {
      try {
        const res = await ctx.kuzu.query(args.query)
        const ids = nodeIdsInRows(ctx, res.rows)
        if (ids.length) ctx.emitViz({ type: 'highlight', nodeIds: ids, note: 'cypher result' })
        return ok({ columns: res.columns, rowCount: res.rows.length, rows: res.rows.slice(0, 200) })
      } catch (e) {
        return err(`Cypher failed: ${(e as Error).message}`)
      }
    }
  )

  const sparql = tool(
    'sparql_query',
    'Run a SPARQL query against the RDF view (Oxigraph). Use this for REASONING over inferred/derived predicates (der:manages, der:reports_to_chain, der:in_department, der:advances_goal_effective) that do not exist in the raw data. Prefixes are auto-injected. Returns rows; entity ids in results are auto-highlighted.',
    { query: z.string().describe('a SPARQL 1.1 query (SELECT/ASK/CONSTRUCT)') },
    async (args) => {
      try {
        const res = ctx.oxigraph.query(args.query)
        const ids = nodeIdsInRows(ctx, res.rows)
        if (ids.length) ctx.emitViz({ type: 'highlight', nodeIds: ids, note: 'sparql result' })
        return ok({ kind: res.kind, boolean: res.boolean, columns: res.columns, rowCount: res.rows.length, rows: res.rows.slice(0, 200) })
      } catch (e) {
        return err(`SPARQL failed: ${(e as Error).message}`)
      }
    }
  )

  const provenance = tool(
    'provenance',
    'Show where a node came from: the source artifacts and the exact text mentions that ground it. Use to justify/cite an answer.',
    { id: z.string() },
    async (args) => ok(ctx.index.provenance(args.id))
  )

  const readArtifact = tool(
    'read_artifact',
    'Read the raw text of a source artifact (markdown/json) by its run-relative path (from provenance).',
    { path: z.string() },
    async (args) => {
      const text = ctx.index.readArtifact(args.path)
      if (text === null) return err(`cannot read ${args.path}`)
      return ok(text.length > 8000 ? text.slice(0, 8000) + '\n…[truncated]' : text)
    }
  )

  const schema = tool(
    'graph_schema',
    'Return the schema for both query engines: node/edge types, the Cypher node/rel tables with their columns, and the SPARQL classes/predicates including the inferred ones. Call this before writing a cypher_query or sparql_query.',
    {},
    async () =>
      ok(
        [
          `NODE TYPES: ${ctx.index.model.nodeTypes.join(', ')}`,
          `EDGE TYPES: ${ctx.index.model.edgeTypes.join(', ')}`,
          '',
          '=== CYPHER (Kùzu) ===',
          ctx.kuzu.describeSchema(),
          '',
          '=== SPARQL (Oxigraph) ===',
          ctx.oxigraph.describeSchema(ctx.index.model)
        ].join('\n')
      )
  )

  const highlight = tool(
    'highlight_nodes',
    'Highlight a specific set of node ids on the graph to visually present your answer to the user. Optionally focus/zoom to them.',
    { nodeIds: z.array(z.string()), note: z.string().optional(), focus: z.boolean().optional() },
    async (args) => {
      const valid = args.nodeIds.filter((id) => ctx.index.getNode(id))
      ctx.emitViz({ type: args.focus ? 'focus' : 'highlight', nodeIds: valid, note: args.note })
      return ok({ highlighted: valid.length })
    }
  )

  return createSdkMcpServer({
    name: 'enterprise-sim-graph',
    version: '0.1.0',
    tools: [schema, search, details, neighbors, path, cypher, sparql, provenance, readArtifact, highlight]
  })
}

export const TOOL_NAMES = [
  'graph_schema',
  'search_nodes',
  'node_details',
  'neighbors',
  'shortest_path',
  'cypher_query',
  'sparql_query',
  'provenance',
  'read_artifact',
  'highlight_nodes'
].map((t) => `mcp__enterprise-sim-graph__${t}`)
