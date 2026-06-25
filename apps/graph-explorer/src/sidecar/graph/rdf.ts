import { createRequire } from 'node:module'
import type { GraphModel } from '../../shared/model.js'

const require = createRequire(import.meta.url)
// eslint-disable-next-line @typescript-eslint/no-var-requires
const oxigraph = require('oxigraph') as typeof import('oxigraph')

export const BASE = 'http://enterprise-sim/'
export const NS = {
  es: BASE,
  cls: `${BASE}class/`,
  rel: `${BASE}rel/`,
  der: `${BASE}derived/`,
  prop: `${BASE}prop/`,
  ent: `${BASE}entity/`
}

export const PREFIXES = `PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX es: <${NS.es}>
PREFIX cls: <${NS.cls}>
PREFIX rel: <${NS.rel}>
PREFIX der: <${NS.der}>
PREFIX prop: <${NS.prop}>
PREFIX ent: <${NS.ent}>`

function sanitizeLocal(key: string): string {
  return key.replace(/[^A-Za-z0-9_]/g, '_')
}

function entIri(id: string): string {
  return NS.ent + encodeURIComponent(id)
}

/**
 * Forward-chaining inference rules, run to a fixpoint (each is an idempotent
 * SPARQL INSERT). Closure rules (transitive) grow the store until stable; this
 * is the entailment that SPARQL buys us over raw Cypher traversal. Derived
 * predicates live in the `der:` namespace so they are distinguishable from the
 * asserted `rel:` edges.
 */
export const INFERENCE_RULES: { name: string; sparql: string }[] = [
  {
    name: 'manages (inverse of reports_to)',
    sparql: `INSERT { ?m der:manages ?p } WHERE { ?p rel:reports_to ?m }`
  },
  {
    name: 'reports_to_chain (transitive base)',
    sparql: `INSERT { ?p der:reports_to_chain ?m } WHERE { ?p rel:reports_to ?m }`
  },
  {
    name: 'reports_to_chain (transitive step)',
    sparql: `INSERT { ?p der:reports_to_chain ?z } WHERE { ?p der:reports_to_chain ?m . ?m der:reports_to_chain ?z }`
  },
  {
    name: 'manages_chain (transitive closure of manages)',
    sparql: `INSERT { ?m der:manages_chain ?p } WHERE { ?p der:reports_to_chain ?m }`
  },
  {
    name: 'collaborates_with (symmetric)',
    sparql: `INSERT { ?b rel:collaborates_with ?a } WHERE { ?a rel:collaborates_with ?b }`
  },
  {
    name: 'subgoal_of_chain (transitive base)',
    sparql: `INSERT { ?a der:subgoal_of_chain ?b } WHERE { ?a rel:subgoal_of ?b }`
  },
  {
    name: 'subgoal_of_chain (transitive step)',
    sparql: `INSERT { ?a der:subgoal_of_chain ?c } WHERE { ?a der:subgoal_of_chain ?b . ?b der:subgoal_of_chain ?c }`
  },
  {
    name: 'subinitiative_of_chain (transitive base)',
    sparql: `INSERT { ?a der:subinitiative_of_chain ?b } WHERE { ?a rel:subinitiative_of ?b }`
  },
  {
    name: 'subinitiative_of_chain (transitive step)',
    sparql: `INSERT { ?a der:subinitiative_of_chain ?c } WHERE { ?a der:subinitiative_of_chain ?b . ?b der:subinitiative_of_chain ?c }`
  },
  {
    name: 'advances_goal_effective (base)',
    sparql: `INSERT { ?x der:advances_goal_effective ?g } WHERE { ?x rel:advances_goal ?g }`
  },
  {
    name: 'advances_goal_effective (propagate up goal tree)',
    sparql: `INSERT { ?x der:advances_goal_effective ?g2 } WHERE { ?x der:advances_goal_effective ?g . ?g der:subgoal_of_chain ?g2 }`
  },
  {
    name: 'in_department (via team membership)',
    sparql: `INSERT { ?p der:in_department ?dept } WHERE { ?p rel:member_of ?team . ?team rel:part_of ?dept }`
  },
  {
    name: 'in_department (via leadership)',
    sparql: `INSERT { ?p der:in_department ?dept } WHERE { ?p rel:leads ?dept . ?dept rdf:type cls:Department }`
  }
]

export interface SparqlResult {
  kind: 'select' | 'ask' | 'construct'
  columns: string[]
  rows: Record<string, string>[]
  boolean?: boolean
}

/** Embedded SPARQL engine: graph -> RDF triples + ontology + materialized inference. */
export class OxigraphEngine {
  private store: import('oxigraph').Store
  inferredCount = 0

  private constructor(store: import('oxigraph').Store) {
    this.store = store
  }

  static build(model: GraphModel): OxigraphEngine {
    const store = new oxigraph.Store()
    const nn = oxigraph.namedNode
    const lit = oxigraph.literal
    const q = oxigraph.quad
    const RDF_TYPE = nn('http://www.w3.org/1999/02/22-rdf-syntax-ns#type')
    const RDFS_LABEL = nn('http://www.w3.org/2000/01/rdf-schema#label')

    for (const n of model.nodes) {
      const s = nn(entIri(n.id))
      store.add(q(s, RDF_TYPE, nn(NS.cls + sanitizeLocal(n.type))))
      store.add(q(s, RDFS_LABEL, lit(n.label)))
      for (const [k, v] of Object.entries(n.props)) {
        if (v === null || v === undefined) continue
        const val = typeof v === 'string' ? v : typeof v === 'number' || typeof v === 'boolean' ? String(v) : null
        if (val === null) continue // skip arrays/objects in RDF (kept in Cypher)
        store.add(q(s, nn(NS.prop + sanitizeLocal(k)), lit(val)))
      }
    }
    for (const e of model.edges) {
      store.add(q(nn(entIri(e.src)), nn(NS.rel + sanitizeLocal(e.type)), nn(entIri(e.dst))))
    }

    const engine = new OxigraphEngine(store)
    engine.materialize()
    return engine
  }

  /** Run inference rules to a fixpoint (store size stops growing). */
  private materialize(): void {
    const before = this.store.size
    let last = -1
    let guard = 0
    while (this.store.size !== last && guard < 50) {
      last = this.store.size
      for (const rule of INFERENCE_RULES) {
        this.store.update(`${PREFIXES}\n${rule.sparql}`)
      }
      guard++
    }
    this.inferredCount = this.store.size - before
  }

  query(sparql: string): SparqlResult {
    const withPrefixes = /\bPREFIX\b/i.test(sparql) ? sparql : `${PREFIXES}\n${sparql}`
    const res = this.store.query(withPrefixes)
    if (typeof res === 'boolean') {
      return { kind: 'ask', columns: [], rows: [], boolean: res }
    }
    const arr = res as unknown[]
    // CONSTRUCT/DESCRIBE -> array of quads (have subject/predicate/object)
    if (arr.length && (arr[0] as { subject?: unknown }).subject) {
      const rows = (arr as import('oxigraph').Quad[]).map((qd) => ({
        subject: this.short(qd.subject.value),
        predicate: this.short(qd.predicate.value),
        object: this.short(qd.object.value)
      }))
      return { kind: 'construct', columns: ['subject', 'predicate', 'object'], rows }
    }
    // SELECT -> array of binding Maps (native Map<variable, Term>)
    const bindings = arr as Map<string, import('oxigraph').Term>[]
    const columns: string[] = bindings.length ? [...bindings[0].keys()] : []
    const rows = bindings.map((b) => {
      const row: Record<string, string> = {}
      for (const k of b.keys()) {
        const term = b.get(k)
        row[k] = term ? this.short(term.value) : ''
      }
      return row
    })
    return { kind: 'select', columns, rows }
  }

  /** Map IRIs back to readable entity ids / prefixed names for display. */
  private short(value: string): string {
    if (value.startsWith(NS.ent)) return decodeURIComponent(value.slice(NS.ent.length))
    if (value.startsWith(NS.rel)) return 'rel:' + value.slice(NS.rel.length)
    if (value.startsWith(NS.der)) return 'der:' + value.slice(NS.der.length)
    if (value.startsWith(NS.cls)) return 'cls:' + value.slice(NS.cls.length)
    if (value.startsWith(NS.prop)) return 'prop:' + value.slice(NS.prop.length)
    return value
  }

  describeSchema(model: GraphModel): string {
    const classes = model.nodeTypes.map((t) => `cls:${t}`).join(', ')
    const rels = model.edgeTypes.map((t) => `rel:${t}`).join(', ')
    const derived = [
      'der:manages',
      'der:manages_chain',
      'der:reports_to_chain',
      'der:subgoal_of_chain',
      'der:subinitiative_of_chain',
      'der:advances_goal_effective',
      'der:in_department'
    ].join(', ')
    return [
      `Entity IRIs: ent:<urlencoded-id>  (e.g. ent:person%3Aben-cho)`,
      `Classes (rdf:type): ${classes}`,
      `Asserted predicates: ${rels}`,
      `Inferred predicates (materialized via the ontology): ${derived}`,
      `Literal props: prop:<key> (e.g. prop:name, prop:seniority, prop:kind)`,
      `Labels: rdfs:label`,
      `Inferred triples added by reasoning: ${this.inferredCount}`
    ].join('\n')
  }
}
