import { useState } from 'react'
import { useStore } from '../store.js'

type Engine = 'cypher' | 'sparql'

const SAMPLES: Record<Engine, { label: string; q: string }[]> = {
  cypher: [
    { label: 'reporting lines', q: 'MATCH (p:Person)-[:reports_to]->(m:Person)\nRETURN p.label AS report, m.label AS manager' },
    { label: 'management chain (var-length)', q: 'MATCH (p:Person)-[:reports_to*1..]->(m:Person)\nRETURN p.label AS person, m.label AS above' },
    { label: 'artifacts & authors', q: 'MATCH (p:Person)-[:authored]->(a:Artifact)\nRETURN p.label AS author, a.label AS artifact' },
    { label: 'goals each initiative advances', q: 'MATCH (i:Initiative)-[:advances_goal]->(g:Goal)\nRETURN i.label AS initiative, g.label AS goal' }
  ],
  sparql: [
    {
      label: 'transitive management (inferred)',
      q: 'SELECT ?person ?above WHERE {\n  ?p der:reports_to_chain ?m .\n  ?p rdfs:label ?person . ?m rdfs:label ?above .\n} ORDER BY ?person'
    },
    {
      label: 'who is in each department (inferred)',
      q: 'SELECT ?person ?dept WHERE {\n  ?p der:in_department ?d .\n  ?p rdfs:label ?person . ?d rdfs:label ?dept .\n}'
    },
    {
      label: 'effective goal advancement (inferred)',
      q: 'SELECT ?x ?goal WHERE {\n  ?n der:advances_goal_effective ?g .\n  ?n rdfs:label ?x . ?g rdfs:label ?goal .\n}'
    },
    { label: 'who manages whom (inferred)', q: 'SELECT ?mgr ?report WHERE {\n  ?m der:manages ?p .\n  ?m rdfs:label ?mgr . ?p rdfs:label ?report .\n}' }
  ]
}

interface Result {
  columns: string[]
  rows: Record<string, unknown>[]
  rowCount?: number
  boolean?: boolean
  kind?: string
}

export function QueryConsole(): JSX.Element {
  const [engine, setEngine] = useState<Engine>('cypher')
  const [text, setText] = useState(SAMPLES.cypher[0].q)
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const runPath = useStore((s) => s.runPath)
  const schema = useStore((s) => s.schema)
  const setHighlight = useStore((s) => s.setHighlight)
  const focusNodes = useStore((s) => s.focusNodes)
  const model = useStore((s) => s.model)

  const run = async () => {
    const rpc = useStore.getState().rpc
    if (!rpc || !runPath) return
    setBusy(true)
    setError(null)
    try {
      const res = await rpc.call<Result>(engine, { runPath, query: text })
      setResult(res)
      // highlight any node ids appearing in results
      if (model) {
        const ids = new Set<string>()
        const known = new Set(model.nodes.map((n) => n.id))
        for (const row of res.rows) for (const v of Object.values(row)) if (typeof v === 'string' && known.has(v)) ids.add(v)
        if (ids.size) {
          setHighlight([...ids])
          focusNodes([...ids], true)
        }
      }
    } catch (e) {
      setError((e as Error).message)
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  const switchEngine = (e: Engine) => {
    setEngine(e)
    setText(SAMPLES[e][0].q)
    setResult(null)
    setError(null)
  }

  return (
    <div className="query-console">
      <div className="engine-tabs">
        <button className={engine === 'cypher' ? 'active' : ''} onClick={() => switchEngine('cypher')}>
          Cypher
        </button>
        <button className={engine === 'sparql' ? 'active' : ''} onClick={() => switchEngine('sparql')}>
          SPARQL (reasoning)
        </button>
      </div>

      <div className="sample-chips">
        {SAMPLES[engine].map((s) => (
          <button key={s.label} className="example-chip small" onClick={() => setText(s.q)}>
            {s.label}
          </button>
        ))}
      </div>

      <textarea className="query-text mono" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
      <div className="btn-row">
        <button className="btn primary" onClick={run} disabled={busy}>
          {busy ? 'running…' : `Run ${engine}`}
        </button>
        <button className="btn ghost" onClick={() => navigator.clipboard.writeText(text)}>
          Copy
        </button>
      </div>

      {error && <div className="msg-error">⚠ {error}</div>}
      {result && (
        <div className="result-block">
          <div className="muted small">
            {result.kind === 'ask'
              ? `ASK → ${result.boolean}`
              : `${result.rowCount ?? result.rows.length} rows`}
          </div>
          {result.rows.length > 0 && (
            <div className="result-scroll">
              <table className="result-table">
                <thead>
                  <tr>
                    {result.columns.map((c) => (
                      <th key={c}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.slice(0, 200).map((row, i) => (
                    <tr key={i}>
                      {result.columns.map((c) => (
                        <td key={c} className="mono">
                          {fmt(row[c])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <details className="block schema-ref">
        <summary>Schema reference</summary>
        <pre className="mono small">
          {engine === 'cypher' ? schema?.kuzuSchema : schema?.sparqlSchema}
        </pre>
      </details>
    </div>
  )
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
