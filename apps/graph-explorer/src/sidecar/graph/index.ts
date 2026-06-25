import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import type { GraphModel, KGNode, KGEdge, KGMention } from '../../shared/model.js'

export interface SubgraphResult {
  nodeIds: string[]
  edgeIds: string[]
}

export interface SearchHit {
  id: string
  type: string
  label: string
  score: number
  matched: string // which field matched
}

export interface ProvenanceResult {
  nodeId: string
  label: string
  mentions: {
    artifact_path: string
    surface_form: string
    line: number
    offset: number
    length: number
    snippet: string | null
  }[]
  artifacts: string[]
}

/**
 * In-memory adjacency + lookup over a loaded run. This backs the deterministic
 * graph tools (search / neighbors / paths / provenance) — distinct from the
 * Cypher (Kùzu) and SPARQL (Oxigraph) engines, which back free-form queries.
 */
export class GraphIndex {
  readonly model: GraphModel
  private nodeById = new Map<string, KGNode>()
  private edgeById = new Map<string, KGEdge>()
  private outAdj = new Map<string, KGEdge[]>()
  private inAdj = new Map<string, KGEdge[]>()
  private mentionsByEntity = new Map<string, KGMention[]>()

  constructor(model: GraphModel) {
    this.model = model
    for (const n of model.nodes) this.nodeById.set(n.id, n)
    for (const e of model.edges) {
      this.edgeById.set(e.id, e)
      if (!this.outAdj.has(e.src)) this.outAdj.set(e.src, [])
      if (!this.inAdj.has(e.dst)) this.inAdj.set(e.dst, [])
      this.outAdj.get(e.src)!.push(e)
      this.inAdj.get(e.dst)!.push(e)
    }
    for (const m of model.mentions) {
      if (!this.mentionsByEntity.has(m.entity_id)) this.mentionsByEntity.set(m.entity_id, [])
      this.mentionsByEntity.get(m.entity_id)!.push(m)
    }
  }

  getNode(id: string): KGNode | undefined {
    return this.nodeById.get(id)
  }

  /** Fuzzy-ish substring search across id, label, aliases, and stringified scalar props. */
  search(query: string, opts: { types?: string[]; limit?: number } = {}): SearchHit[] {
    const q = query.trim().toLowerCase()
    const limit = opts.limit ?? 50
    if (!q) return []
    const typeFilter = opts.types && opts.types.length ? new Set(opts.types) : null
    const hits: SearchHit[] = []
    for (const n of this.model.nodes) {
      if (typeFilter && !typeFilter.has(n.type)) continue
      let score = 0
      let matched = ''
      const label = n.label.toLowerCase()
      if (label === q) {
        score = 100
        matched = 'label'
      } else if (label.includes(q)) {
        score = 70 - (label.length - q.length) * 0.1
        matched = 'label'
      } else if (n.id.toLowerCase().includes(q)) {
        score = 50
        matched = 'id'
      } else if (n.aliases.some((a) => a.toLowerCase().includes(q))) {
        score = 45
        matched = 'alias'
      } else {
        for (const [k, v] of Object.entries(n.props)) {
          if (typeof v === 'string' && v.toLowerCase().includes(q)) {
            score = 30
            matched = `props.${k}`
            break
          }
        }
      }
      if (score > 0) hits.push({ id: n.id, type: n.type, label: n.label, score, matched })
    }
    hits.sort((a, b) => b.score - a.score || a.label.localeCompare(b.label))
    return hits.slice(0, limit)
  }

  /**
   * Subgraph of all nodes within `depth` hops of `nodeId`, plus the connecting
   * edges. `direction` controls which edges are followed; `edgeTypes` filters.
   */
  neighbors(
    nodeId: string,
    opts: { depth?: number; edgeTypes?: string[]; direction?: 'out' | 'in' | 'both' } = {}
  ): SubgraphResult {
    const depth = Math.max(1, Math.min(opts.depth ?? 1, 6))
    const direction = opts.direction ?? 'both'
    const typeFilter = opts.edgeTypes && opts.edgeTypes.length ? new Set(opts.edgeTypes) : null
    const nodeIds = new Set<string>([nodeId])
    const edgeIds = new Set<string>()
    let frontier = [nodeId]
    for (let d = 0; d < depth; d++) {
      const next: string[] = []
      for (const cur of frontier) {
        const candidates: KGEdge[] = []
        if (direction === 'out' || direction === 'both') candidates.push(...(this.outAdj.get(cur) ?? []))
        if (direction === 'in' || direction === 'both') candidates.push(...(this.inAdj.get(cur) ?? []))
        for (const e of candidates) {
          if (typeFilter && !typeFilter.has(e.type)) continue
          edgeIds.add(e.id)
          const other = e.src === cur ? e.dst : e.src
          if (!nodeIds.has(other)) {
            nodeIds.add(other)
            next.push(other)
          }
        }
      }
      frontier = next
      if (!frontier.length) break
    }
    return { nodeIds: [...nodeIds], edgeIds: [...edgeIds] }
  }

  /** BFS shortest path (treats edges as undirected unless `directed`). */
  shortestPath(
    from: string,
    to: string,
    opts: { directed?: boolean; edgeTypes?: string[] } = {}
  ): { nodeIds: string[]; edgeIds: string[] } | null {
    if (from === to) return { nodeIds: [from], edgeIds: [] }
    const typeFilter = opts.edgeTypes && opts.edgeTypes.length ? new Set(opts.edgeTypes) : null
    const prev = new Map<string, { node: string; edge: string }>()
    const visited = new Set<string>([from])
    let frontier = [from]
    while (frontier.length) {
      const next: string[] = []
      for (const cur of frontier) {
        const out = this.outAdj.get(cur) ?? []
        const inc = opts.directed ? [] : this.inAdj.get(cur) ?? []
        for (const e of [...out, ...inc]) {
          if (typeFilter && !typeFilter.has(e.type)) continue
          const other = e.src === cur ? e.dst : e.src
          if (visited.has(other)) continue
          visited.add(other)
          prev.set(other, { node: cur, edge: e.id })
          if (other === to) {
            // reconstruct
            const nodeIds = [to]
            const edgeIds: string[] = []
            let c = to
            while (c !== from) {
              const step = prev.get(c)!
              edgeIds.unshift(step.edge)
              nodeIds.unshift(step.node)
              c = step.node
            }
            return { nodeIds, edgeIds }
          }
          next.push(other)
        }
      }
      frontier = next
    }
    return null
  }

  /** Mentions + source-artifact snippets that ground a node (the provenance trail). */
  provenance(nodeId: string): ProvenanceResult {
    const node = this.nodeById.get(nodeId)
    const ms = this.mentionsByEntity.get(nodeId) ?? []
    const mentions = ms.map((m) => ({
      artifact_path: m.artifact_path,
      surface_form: m.surface_form,
      line: m.locator.line,
      offset: m.locator.offset,
      length: m.locator.length,
      snippet: this.snippet(m)
    }))
    const fromProv = this.model.provenance
      .filter((p) => p.target_id === nodeId)
      .flatMap((p) => p.artifacts.map((a) => a.path))
    const artifacts = [...new Set([...ms.map((m) => m.artifact_path), ...fromProv])]
    return { nodeId, label: node?.label ?? nodeId, mentions, artifacts }
  }

  /** Read raw artifact text (markdown / json) relative to the run root. */
  readArtifact(relPath: string): string | null {
    const full = join(this.model.runPath, relPath)
    if (!existsSync(full)) return null
    try {
      return readFileSync(full, 'utf8')
    } catch {
      return null
    }
  }

  private snippet(m: KGMention): string | null {
    const text = this.readArtifact(m.artifact_path)
    if (text === null) return null
    const lines = text.split('\n')
    const i = Math.max(0, m.locator.line - 1)
    return lines[i] ?? null
  }
}
