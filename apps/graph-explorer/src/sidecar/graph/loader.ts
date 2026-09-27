import { readFileSync, existsSync } from 'node:fs'
import { join, basename } from 'node:path'
import type {
  GraphModel,
  KGNode,
  KGEdge,
  KGMention,
  KGEvent,
  KGProvenance,
  KGAlias,
  ValidationIssue
} from '../../shared/model.js'

function readJsonl<T>(path: string): T[] {
  if (!existsSync(path)) return []
  const text = readFileSync(path, 'utf8')
  const out: T[] = []
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t) continue
    try {
      out.push(JSON.parse(t) as T)
    } catch {
      // skip malformed line
    }
  }
  return out
}

function readJson<T>(path: string): T | null {
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as T
  } catch {
    return null
  }
}

/** Best human-readable label for a node. */
export function deriveLabel(n: { props: Record<string, unknown>; aliases: string[]; id: string }): string {
  const p = n.props ?? {}
  for (const key of ['title', 'name', 'statement', 'issue_key', 'kind']) {
    const v = p[key]
    if (typeof v === 'string' && v.trim()) return v
  }
  if (n.aliases?.length && n.aliases[0].trim()) return n.aliases[0]
  return n.id
}

/**
 * Load a world directory's gold KG into the canonical GraphModel.
 * `worldPath` is the world root (the dir containing `.world`, `kg/`, ...).
 */
export function loadWorld(worldPath: string): GraphModel {
  const kgDir = join(worldPath, 'kg')
  const rawNodes = readJsonl<Omit<KGNode, 'label'>>(join(kgDir, 'nodes.jsonl'))
  const nodes: KGNode[] = rawNodes.map((n) => ({ ...n, label: deriveLabel(n) }))
  const edges = readJsonl<KGEdge>(join(kgDir, 'edges.jsonl'))
  const mentions = readJsonl<KGMention>(join(kgDir, 'mentions.jsonl'))
  const events = readJsonl<KGEvent>(join(kgDir, 'events.jsonl'))
  const provenance = readJsonl<KGProvenance>(join(kgDir, 'provenance.jsonl'))
  const aliases = readJsonl<KGAlias>(join(kgDir, 'aliases.jsonl'))
  const validation = readJsonl<ValidationIssue>(join(worldPath, 'validation', 'issues.jsonl'))
  const world = readJson<Record<string, unknown>>(join(worldPath, '.world'))

  const nodeTypes = [...new Set(nodes.map((n) => n.type))].sort()
  const edgeTypes = [...new Set(edges.map((e) => e.type))].sort()

  // Time window: prefer the .world's window, else min/max of event timestamps.
  let timeRange: GraphModel['timeRange'] = null
  const win = (world?.window ?? null) as { start?: string; end?: string } | null
  if (win?.start && win?.end) {
    timeRange = { start: win.start, end: win.end }
  } else if (events.length) {
    const ts = events.map((e) => e.timestamp).filter(Boolean).sort()
    if (ts.length) timeRange = { start: ts[0], end: ts[ts.length - 1] }
  }

  return {
    worldId: typeof world?.world_id === 'string' ? world.world_id : basename(worldPath),
    worldPath,
    nodes,
    edges,
    mentions,
    events,
    provenance,
    aliases,
    validation,
    world,
    nodeTypes,
    edgeTypes,
    timeRange
  }
}
