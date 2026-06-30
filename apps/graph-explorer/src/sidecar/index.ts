import { WebSocketServer, type WebSocket } from 'ws'
import { readdirSync, existsSync, statSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Load apps/graph-explorer/.env.local (gitignored) so a locally-provided
// ANTHROPIC_API_KEY reaches the Agent SDK without env plumbing. Built-in, no dep.
const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const ENV_FILE = join(APP_ROOT, '.env.local')
if (existsSync(ENV_FILE)) {
  try {
    process.loadEnvFile(ENV_FILE)
  } catch {
    /* ignore malformed env file */
  }
}
import type { RpcRequest, ServerMessage, LoadRunResult, DiffResult } from '../shared/protocol.js'
import type { RunSummary } from '../shared/model.js'
import { loadRun } from './graph/loader.js'
import { diffModels } from './graph/diff.js'
import { GraphIndex } from './graph/index.js'
import { KuzuEngine } from './graph/kuzu.js'
import { OxigraphEngine } from './graph/rdf.js'
import { runChat, type ChatEngines } from './agent/harness.js'

const RUNS_ROOT = process.env.GRAPH_EXPLORER_RUNS_ROOT ?? join(process.cwd(), '..', '..', 'runs')
const PORT = Number(process.env.GRAPH_EXPLORER_PORT ?? 0)

/** One fully-loaded run: model + index + both query engines. */
interface LoadedRun extends ChatEngines {
  result: LoadRunResult
}

const loaded = new Map<string, LoadedRun>() // runPath -> loaded
const chatAborts = new Map<string, AbortController>() // request id -> controller
const chatSessions = new Map<string, string>() // ws-scoped conversation id -> sdk session id

function isRunDir(p: string): boolean {
  return existsSync(join(p, 'kg', 'nodes.jsonl'))
}

function discoverRuns(root: string): RunSummary[] {
  const out: RunSummary[] = []
  const walk = (dir: string, depth: number) => {
    if (depth > 3 || !existsSync(dir)) return
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }
    if (isRunDir(dir)) {
      try {
        const m = loadRun(dir)
        out.push({
          runId: m.runId,
          runPath: dir,
          nodeCount: m.nodes.length,
          edgeCount: m.edges.length,
          window: m.timeRange
        })
      } catch {
        /* skip */
      }
      return // don't descend into a run
    }
    for (const e of entries) {
      const full = join(dir, e)
      try {
        if (statSync(full).isDirectory()) walk(full, depth + 1)
      } catch {
        /* skip */
      }
    }
  }
  walk(root, 0)
  return out.sort((a, b) => a.runPath.localeCompare(b.runPath))
}

async function ensureLoaded(runPath: string): Promise<LoadedRun> {
  const existing = loaded.get(runPath)
  if (existing) return existing
  const model = loadRun(runPath)
  const index = new GraphIndex(model)
  const kuzu = await KuzuEngine.build(model)
  const oxigraph = OxigraphEngine.build(model)
  const result: LoadRunResult = {
    model,
    kuzuSchema: kuzu.describeSchema(),
    sparqlSchema: oxigraph.describeSchema(model),
    inferredCount: oxigraph.inferredCount,
    eval: summarizeEval(model)
  }
  const lr: LoadedRun = { index, kuzu, oxigraph, result }
  loaded.set(runPath, lr)
  return lr
}

/** Surface manifest validation + any committed eval as lightweight metrics. */
function summarizeEval(model: ReturnType<typeof loadRun>): LoadRunResult['eval'] {
  const metrics: { name: string; score: number; ok: boolean; detail?: string }[] = []
  const byKind = new Map<string, number>()
  for (const v of model.validation) byKind.set(v.kind, (byKind.get(v.kind) ?? 0) + 1)
  const hard = [...byKind].filter(([k]) => !k.startsWith('unresolved')).reduce((a, [, c]) => a + c, 0)
  metrics.push({
    name: 'consistency (hard issues)',
    score: hard === 0 ? 1 : 0,
    ok: hard === 0,
    detail: hard === 0 ? 'no hard inconsistencies' : `${hard} hard issues`
  })
  const totalMentions = model.mentions.length
  const unresolved = byKind.get('unresolved_mention') ?? 0
  const resolveRate = totalMentions ? 1 - unresolved / totalMentions : 1
  metrics.push({
    name: 'mention resolution',
    score: resolveRate,
    ok: resolveRate > 0.9,
    detail: `${totalMentions - unresolved}/${totalMentions} mentions resolved`
  })
  return { metrics, ok: metrics.every((m) => m.ok) }
}

function diffRuns(a: LoadedRun, b: LoadedRun): DiffResult {
  return diffModels(a.index.model, b.index.model)
}

async function handle(ws: WebSocket, req: RpcRequest): Promise<void> {
  const send = (m: ServerMessage): void => {
    if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(m))
  }
  const reply = (ok: boolean, result?: unknown, error?: string): void =>
    send({ type: 'rpc_result', id: req.id, ok, result, error })
  const p = (req.params ?? {}) as Record<string, unknown>

  try {
    switch (req.op) {
      case 'listRuns':
        return reply(true, discoverRuns((p.root as string) ?? RUNS_ROOT))
      case 'loadRun': {
        const lr = await ensureLoaded(p.path as string)
        return reply(true, lr.result)
      }
      case 'search': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(true, lr.index.search(p.query as string, { types: p.types as string[], limit: p.limit as number }))
      }
      case 'neighbors': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(
          true,
          lr.index.neighbors(p.id as string, {
            depth: p.depth as number,
            edgeTypes: p.edgeTypes as string[],
            direction: p.direction as 'out' | 'in' | 'both'
          })
        )
      }
      case 'shortestPath': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(true, lr.index.shortestPath(p.from as string, p.to as string, { directed: p.directed as boolean }))
      }
      case 'provenance': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(true, lr.index.provenance(p.id as string))
      }
      case 'readArtifact': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(true, { text: lr.index.readArtifact(p.path as string) })
      }
      case 'cypher': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(true, await lr.kuzu.query(p.query as string))
      }
      case 'sparql': {
        const lr = await ensureLoaded(p.runPath as string)
        return reply(true, lr.oxigraph.query(p.query as string))
      }
      case 'diffRuns': {
        const a = await ensureLoaded(p.pathA as string)
        const b = await ensureLoaded(p.pathB as string)
        return reply(true, diffRuns(a, b))
      }
      case 'chat': {
        const lr = await ensureLoaded(p.runPath as string)
        const convoKey = (p.conversationId as string) ?? req.id
        const ac = new AbortController()
        chatAborts.set(req.id, ac)
        await runChat(lr, {
          prompt: p.prompt as string,
          model: p.model as string,
          resume: chatSessions.get(convoKey) ?? null,
          signal: ac.signal,
          onEvent: (event) => {
            if (event.kind === 'done' && event.sessionId) chatSessions.set(convoKey, event.sessionId)
            send({ type: 'stream', id: req.id, event })
          }
        })
        chatAborts.delete(req.id)
        return reply(true, { finished: true })
      }
      case 'cancelChat': {
        chatAborts.get(p.id as string)?.abort()
        return reply(true, { cancelled: true })
      }
      default:
        return reply(false, undefined, `unknown op: ${req.op}`)
    }
  } catch (e) {
    return reply(false, undefined, (e as Error).message)
  }
}

const wss = new WebSocketServer({ host: '127.0.0.1', port: PORT })
wss.on('connection', (ws) => {
  ws.on('message', (data) => {
    let req: RpcRequest
    try {
      req = JSON.parse(data.toString())
    } catch {
      return
    }
    if (req?.type === 'rpc') void handle(ws, req)
  })
})
wss.on('listening', () => {
  const addr = wss.address()
  const port = typeof addr === 'object' && addr ? addr.port : PORT
  // main process parses this line to learn where to connect
  process.stdout.write(`SIDECAR_PORT=${port}\n`)
})
wss.on('error', (e) => {
  process.stderr.write(`sidecar error: ${e.message}\n`)
  process.exit(1)
})

process.on('SIGTERM', () => process.exit(0))
process.on('SIGINT', () => process.exit(0))
