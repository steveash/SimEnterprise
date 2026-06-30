// End-to-end integration test for the sidecar's WebSocket RPC surface.
//
// Rather than import the handlers, this spawns the REAL sidecar (src/sidecar/index.ts
// under tsx) and drives it over a localhost WebSocket exactly as the renderer's Rpc
// client does — proving the wire protocol, the native query engines (Kùzu/Oxigraph),
// and the graph index all work together in the process that actually ships.
//
// No ANTHROPIC_API_KEY is required: every op exercised here is deterministic. The
// `chat`/`cancelChat` ops (which call the Agent SDK) are intentionally left out.
//
// Skips cleanly (exit 0) on a checkout without the golden run under `runs/`.

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { WebSocket } from 'ws'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { GOLDEN_RUN, goldenRunExists } from './helpers.js'
import type { RpcResponse } from '../src/shared/protocol.js'
import type { LoadRunResult, DiffResult } from '../src/shared/protocol.js'
import type { RunSummary } from '../src/shared/model.js'
import type { SearchHit, SubgraphResult, ProvenanceResult } from '../src/sidecar/graph/index.js'
import type { CypherResult } from '../src/sidecar/graph/kuzu.js'
import type { SparqlResult } from '../src/sidecar/graph/rdf.js'

const APP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SIDECAR = join(APP_ROOT, 'src', 'sidecar', 'index.ts')
const TSX = join(APP_ROOT, 'node_modules', '.bin', 'tsx')
// Repo-root `runs/` (two levels above the app) — what listRuns should discover.
const RUNS_ROOT = join(GOLDEN_RUN, '..', '..')

// Engines build on the first loadRun; give every test plenty of headroom.
const TEST_TIMEOUT = 60_000

/** Minimal RPC client mirroring renderer/rpc.ts: id-correlated request/reply. */
class TestRpc {
  private ws: WebSocket
  private pending = new Map<string, { resolve: (r: RpcResponse) => void }>()
  private counter = 0
  ready: Promise<void>

  constructor(port: number) {
    this.ws = new WebSocket(`ws://127.0.0.1:${port}`)
    this.ready = new Promise((res, rej) => {
      this.ws.on('open', () => res())
      this.ws.on('error', (e) => rej(e))
    })
    this.ws.on('message', (data) => {
      const msg = JSON.parse(data.toString()) as RpcResponse
      if (msg.type !== 'rpc_result') return
      const p = this.pending.get(msg.id)
      if (!p) return
      this.pending.delete(msg.id)
      p.resolve(msg)
    })
  }

  /** Send an op and resolve with the full response (ok flag intact). */
  send(op: string, params?: unknown): Promise<RpcResponse> {
    const id = `t${++this.counter}`
    return new Promise((resolve) => {
      this.pending.set(id, { resolve })
      this.ws.send(JSON.stringify({ type: 'rpc', id, op, params }))
    })
  }

  /** Like renderer's call(): resolve with result on ok, reject on error. */
  async call<T = unknown>(op: string, params?: unknown): Promise<T> {
    const msg = await this.send(op, params)
    if (!msg.ok) throw new Error(msg.error ?? 'rpc error')
    return msg.result as T
  }

  close(): void {
    this.ws.close()
  }
}

describe.skipIf(!goldenRunExists())('sidecar WebSocket RPC', () => {
  let proc: ChildProcessWithoutNullStreams
  let rpc: TestRpc

  beforeAll(async () => {
    proc = spawn(TSX, [SIDECAR], {
      cwd: APP_ROOT,
      env: {
        ...process.env,
        GRAPH_EXPLORER_RUNS_ROOT: RUNS_ROOT,
        GRAPH_EXPLORER_PORT: '0'
      }
    })
    proc.stderr.on('data', (d) => process.stderr.write(`[sidecar] ${d}`))

    // Wait for the sidecar to announce its port on stdout.
    const port = await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('sidecar did not report a port')), 30_000)
      let buf = ''
      proc.stdout.on('data', (d) => {
        buf += d.toString()
        const m = buf.match(/SIDECAR_PORT=(\d+)/)
        if (m) {
          clearTimeout(timer)
          resolve(Number(m[1]))
        }
      })
      proc.on('exit', (code) => reject(new Error(`sidecar exited early (code ${code})`)))
    })

    rpc = new TestRpc(port)
    await rpc.ready
  }, TEST_TIMEOUT)

  afterAll(() => {
    rpc?.close()
    proc?.kill('SIGTERM')
  })

  it(
    'listRuns discovers the golden run',
    async () => {
      const runs = await rpc.call<RunSummary[]>('listRuns')
      expect(runs.length).toBeGreaterThanOrEqual(1)
      const golden = runs.find((r) => r.runPath === GOLDEN_RUN)
      expect(golden, 'golden run should be discovered under RUNS_ROOT').toBeTruthy()
      expect(golden!.nodeCount).toBeGreaterThan(0)
      expect(golden!.edgeCount).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  it(
    'loadRun loads the model, infers triples, and evaluates clean',
    async () => {
      const res = await rpc.call<LoadRunResult>('loadRun', { path: GOLDEN_RUN })
      expect(res.model.nodes.length).toBeGreaterThan(0)
      expect(res.model.edges.length).toBeGreaterThan(0)
      // Oxigraph materializes ontology inferences on load.
      expect(res.inferredCount).toBeGreaterThan(0)
      expect(res.kuzuSchema).toContain('NODE TABLES')
      expect(res.sparqlSchema.length).toBeGreaterThan(0)
      expect(res.eval).not.toBeNull()
      expect(res.eval!.ok).toBe(true)
    },
    TEST_TIMEOUT
  )

  it(
    'search resolves a known entity',
    async () => {
      const hits = await rpc.call<SearchHit[]>('search', { runPath: GOLDEN_RUN, query: 'Cleo' })
      expect(hits.length).toBeGreaterThan(0)
      expect(hits[0]).toHaveProperty('id')
      expect(hits[0]).toHaveProperty('score')
      expect(hits.some((h) => h.id.startsWith('person:'))).toBe(true)
    },
    TEST_TIMEOUT
  )

  it(
    'neighbors returns a subgraph around a node',
    async () => {
      const sub = await rpc.call<SubgraphResult>('neighbors', {
        runPath: GOLDEN_RUN,
        id: 'person:cleo-diaz',
        depth: 1
      })
      expect(sub.nodeIds).toContain('person:cleo-diaz')
      expect(sub.nodeIds.length).toBeGreaterThan(1)
      expect(sub.edgeIds.length).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  it(
    'cypher runs a traversal query',
    async () => {
      const res = await rpc.call<CypherResult>('cypher', {
        runPath: GOLDEN_RUN,
        query: 'MATCH (p:Person) RETURN p.id AS id, p.label AS label'
      })
      expect(res.columns).toEqual(['id', 'label'])
      expect(res.rows.length).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  it(
    'sparql runs a reasoning query',
    async () => {
      const res = await rpc.call<SparqlResult>('sparql', {
        runPath: GOLDEN_RUN,
        query: 'SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5'
      })
      expect(res.kind).toBe('select')
      expect(res.rows.length).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  it(
    'provenance grounds a node in source mentions',
    async () => {
      const prov = await rpc.call<ProvenanceResult>('provenance', {
        runPath: GOLDEN_RUN,
        id: 'person:cleo-diaz'
      })
      expect(prov.nodeId).toBe('person:cleo-diaz')
      expect(prov.mentions.length).toBeGreaterThan(0)
      expect(prov.artifacts.length).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  it(
    'diffRuns of a run against itself shows zero drift',
    async () => {
      const diff = await rpc.call<DiffResult>('diffRuns', { pathA: GOLDEN_RUN, pathB: GOLDEN_RUN })
      expect(diff.nodes.added).toEqual([])
      expect(diff.nodes.removed).toEqual([])
      expect(diff.edges.added).toEqual([])
      expect(diff.edges.removed).toEqual([])
      // Everything is common to both sides.
      expect(diff.nodes.common.length).toBe(diff.a.nodeCount)
      expect(diff.edges.common.length).toBe(diff.a.edgeCount)
      // Typed breakdown fields are present over the wire and empty for a self-diff.
      expect(diff.nodeChanges.added).toEqual([])
      expect(diff.nodeChanges.removed).toEqual([])
      expect(diff.edgeChanges.added).toEqual([])
      expect(diff.edgeChanges.removed).toEqual([])
      expect(diff.nodeTypeDeltas).toEqual([])
      expect(diff.edgeTypeDeltas).toEqual([])
    },
    TEST_TIMEOUT
  )

  it(
    'a malformed Cypher query errors without crashing the sidecar',
    async () => {
      const bad = await rpc.send('cypher', { runPath: GOLDEN_RUN, query: 'THIS IS NOT CYPHER' })
      expect(bad.ok).toBe(false)
      expect(bad.error).toBeTruthy()

      // The sidecar must still serve subsequent requests on the same connection.
      const ok = await rpc.call<RunSummary[]>('listRuns')
      expect(ok.length).toBeGreaterThanOrEqual(1)
    },
    TEST_TIMEOUT
  )
})
