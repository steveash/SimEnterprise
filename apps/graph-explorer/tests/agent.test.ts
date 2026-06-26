import { describe, it, expect } from 'vitest'
import { GraphIndex } from '../src/sidecar/graph/index.js'
import { KuzuEngine } from '../src/sidecar/graph/kuzu.js'
import { OxigraphEngine } from '../src/sidecar/graph/rdf.js'
import { buildMcpServer, buildTools, TOOL_NAMES, type ToolContext, type VizEvent } from '../src/sidecar/agent/tools.js'
import { runChat, type AgentEvent } from '../src/sidecar/agent/harness.js'
import { loadGolden, goldenRunExists } from './helpers.js'
import type { GraphModel } from '../src/shared/model.js'

// ---------------------------------------------------------------------------
// NO-KEY part — always runs. The MCP server wiring + tool handlers are pure
// in-memory logic over a GraphIndex, so we drive them with a tiny synthetic
// model and a hand-built ToolContext. No golden run, no Agent SDK loop, no
// ANTHROPIC_API_KEY required — these assertions must hold on any checkout.
// ---------------------------------------------------------------------------

/** Minimal deterministic graph: one Person (a Senior Engineer) on one Team. */
function tinyModel(): GraphModel {
  const now = '2026-01-01T00:00:00Z'
  return {
    runId: 'test-run',
    runPath: '/nonexistent/test-run',
    nodes: [
      { id: 'person:ash', type: 'Person', label: 'Steve Ash', aliases: ['Ash'], created_at: now, props: { title: 'Senior Engineer' } },
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

/** A ToolContext over the tiny model, capturing every emitted VizEvent. */
function tinyContext(): { ctx: ToolContext; viz: VizEvent[] } {
  const viz: VizEvent[] = []
  const ctx: ToolContext = {
    index: new GraphIndex(tinyModel()),
    // Kùzu/Oxigraph engines are not exercised by the handlers under test
    // (search_nodes only touches index + emitViz); stub them for typing.
    kuzu: {} as KuzuEngine,
    oxigraph: {} as OxigraphEngine,
    emitViz: (e) => viz.push(e)
  }
  return { ctx, viz }
}

describe('MCP server wiring', () => {
  it('buildMcpServer returns an sdk server', () => {
    const { ctx } = tinyContext()
    const server = buildMcpServer(ctx)
    expect(server.type).toBe('sdk')
    expect(server.name).toBe('enterprise-sim-graph')
  })

  it('exposes exactly 10 tools', () => {
    const { ctx } = tinyContext()
    expect(buildTools(ctx)).toHaveLength(10)
  })

  it('TOOL_NAMES has 10 entries, all mcp__enterprise-sim-graph__ prefixed', () => {
    expect(TOOL_NAMES).toHaveLength(10)
    for (const name of TOOL_NAMES) {
      expect(name.startsWith('mcp__enterprise-sim-graph__')).toBe(true)
    }
  })

  it('every built tool has a matching prefixed TOOL_NAMES entry', () => {
    const { ctx } = tinyContext()
    const names = buildTools(ctx).map((t) => `mcp__enterprise-sim-graph__${t.name}`)
    expect(new Set(names)).toEqual(new Set(TOOL_NAMES))
  })
})

describe('tool handlers (driven without the SDK)', () => {
  // CallToolResult, as the handlers actually return it (text content only).
  type ToolResult = { content: { type: string; text?: string }[]; isError?: boolean }

  // The tool union types each handler with its own arg shape; we invoke by name
  // dynamically, so erase the per-tool arg type at the call boundary.
  function handlerFor(ctx: ToolContext, name: string): (args: Record<string, unknown>) => Promise<ToolResult> {
    const t = buildTools(ctx).find((t) => t.name === name)
    if (!t) throw new Error(`no tool named ${name}`)
    return t.handler as unknown as (args: Record<string, unknown>) => Promise<ToolResult>
  }

  it('search_nodes returns a CallToolResult and emits a highlight viz event', async () => {
    const { ctx, viz } = tinyContext()
    const result = await handlerFor(ctx, 'search_nodes')({ query: 'Steve' })

    // CallToolResult shape: { content: [{ type: 'text', text: ... }] }
    expect(result.content).toBeInstanceOf(Array)
    expect(result.content[0]).toMatchObject({ type: 'text' })
    expect(typeof result.content[0].text).toBe('string')
    // The hit set is serialized into the text payload.
    expect(result.content[0].text).toContain('person:ash')

    // The handler fired emitViz with a highlight over the matched node.
    const highlights = viz.filter((e) => e.type === 'highlight')
    expect(highlights.length).toBeGreaterThanOrEqual(1)
    expect(highlights[0].nodeIds).toContain('person:ash')
  })

  it('node_details emits a focus viz event for a real id', async () => {
    const { ctx, viz } = tinyContext()
    const result = await handlerFor(ctx, 'node_details')({ id: 'person:ash' })
    expect(result.content[0].text).toContain('Steve Ash')
    expect(viz.some((e) => e.type === 'focus' && e.nodeIds.includes('person:ash'))).toBe(true)
  })

  it('node_details on an unknown id returns an error result and emits nothing', async () => {
    const { ctx, viz } = tinyContext()
    const result = await handlerFor(ctx, 'node_details')({ id: 'person:nobody' })
    expect(result.isError).toBe(true)
    expect(viz).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// GATED part — a real live agent turn against the golden run. Runs only when
// ANTHROPIC_API_KEY is set (and the golden run is present, since the question
// requires real data); otherwise vitest reports it as skipped.
// ---------------------------------------------------------------------------

const liveEnabled = !!process.env.ANTHROPIC_API_KEY && goldenRunExists()

describe.skipIf(!liveEnabled)('live agent turn (gated on ANTHROPIC_API_KEY)', () => {
  it(
    'runs a query tool and reaches a done event',
    async () => {
      const model = loadGolden()
      const index = new GraphIndex(model)
      const kuzu = await KuzuEngine.build(model)
      const oxigraph = OxigraphEngine.build(model)

      const events: AgentEvent[] = []
      await runChat(
        { index, kuzu, oxigraph },
        {
          prompt: 'Who does the Senior Engineer report to? Use a query.',
          model: 'haiku',
          onEvent: (e) => events.push(e)
        }
      )

      const toolUses = events.filter((e): e is Extract<AgentEvent, { kind: 'tool_use' }> => e.kind === 'tool_use')
      const queried = toolUses.some((e) => e.name === 'cypher_query' || e.name === 'sparql_query')
      expect(queried, `expected a cypher_query or sparql_query tool_use; saw: ${toolUses.map((e) => e.name).join(', ')}`).toBe(true)

      expect(events.some((e) => e.kind === 'done')).toBe(true)
    },
    60_000
  )
})
