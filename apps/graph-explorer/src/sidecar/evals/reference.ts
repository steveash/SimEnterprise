// Hand-written reference queries per reasoning type — ported from
// enterprise_sim/benchmark/runners/reference.py so the explorer's in-process
// answerer (answerer.ts) and propose chat (propose.ts) can brief the agent with
// the same worked examples the Python graph-agent runner uses (EXPLORER_EVALS.md
// §3). Entity IRIs use `ent:<urlencoded-id>`, matching src/sidecar/graph/rdf.ts's
// `entIri`.
export interface Reference {
  key: string
  reasoningType: string
  description: string
  cypher: (subjectId: string) => string
  sparql: (subjectId: string) => string
}

function ent(id: string): string {
  return `ent:${encodeURIComponent(id)}`
}

export const REFERENCES: Reference[] = [
  {
    key: 'reports_to',
    reasoningType: 'direct_relation',
    description: 'who a person directly reports to',
    cypher: (s) => `MATCH (p:Person {id: "${s}"})-[:reports_to]->(m) RETURN m.id AS id`,
    sparql: (s) => `SELECT ?m WHERE { ${ent(s)} rel:reports_to ?m }`
  },
  {
    key: 'management_chain',
    reasoningType: 'transitive',
    description: "a person's full management chain (skip-levels included)",
    cypher: (s) => `MATCH (p:Person {id: "${s}"})-[:reports_to*1..]->(m) RETURN DISTINCT m.id AS id`,
    sparql: (s) => `SELECT ?m WHERE { ${ent(s)} der:reports_to_chain ?m }`
  },
  {
    key: 'in_department',
    reasoningType: 'transitive',
    description: 'which department a person sits in (via team membership/leadership)',
    cypher: (s) =>
      `MATCH (p:Person {id: "${s}"})-[:member_of]->(:Team)-[:part_of]->(d:Department) RETURN DISTINCT d.id AS id`,
    sparql: (s) => `SELECT ?d WHERE { ${ent(s)} der:in_department ?d }`
  },
  {
    key: 'team_headcount',
    reasoningType: 'aggregation',
    description: 'the people who are members of a team (count = size of the set)',
    cypher: (s) => `MATCH (p:Person)-[:member_of]->(t {id: "${s}"}) RETURN p.id AS id`,
    sparql: (s) => `SELECT ?p WHERE { ?p rel:member_of ${ent(s)} }`
  },
  {
    key: 'goal_advancers',
    reasoningType: 'goal_tree',
    description: 'everything advancing a goal, directly or via its subgoals',
    cypher: (s) =>
      `MATCH (target)-[:subgoal_of*0..]->(:Goal {id: "${s}"}) MATCH (x)-[:advances_goal]->(target) RETURN DISTINCT x.id AS id`,
    sparql: (s) => `SELECT ?x WHERE { ?x der:advances_goal_effective ${ent(s)} }`
  },
  {
    key: 'provenance',
    reasoningType: 'provenance',
    description: 'which artifacts mention/ground an entity',
    cypher: (s) => `MATCH (a)-[:mentions]->(e {id: "${s}"}) RETURN a.id AS id`,
    sparql: (s) => `SELECT ?a WHERE { ?a rel:mentions ${ent(s)} }`
  }
]

/** Render the worked-example block used in the answerer/propose system prompts. */
export function referenceExamplesBlock(): string {
  return REFERENCES.map(
    (r) =>
      `  - ${r.description} (${r.reasoningType}):\n      Cypher: ${r.cypher('<id>')}\n      SPARQL: ${r.sparql('<id>')}`
  ).join('\n')
}
