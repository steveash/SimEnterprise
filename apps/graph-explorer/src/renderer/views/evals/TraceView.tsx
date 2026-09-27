// A question's full transcript (EvalTrace): every thinking block, text block,
// tool call (with its input) and tool result, in order, plus the outcome —
// the "what actually happened" view for a single eval answer. Used by the
// Questions tab (most recent trace) and the Executions tab drill-down.
import { useState } from 'react'
import type { AgentEvent } from '../../../shared/agent-events.js'
import type { EvalTrace } from '../../../shared/evals-protocol.js'

function pretty(v: unknown): string {
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

/** Pair each tool_use with its tool_result so a call and its answer render together. */
function steps(events: AgentEvent[]): Array<
  | { kind: 'thinking' | 'text'; text: string }
  | { kind: 'tool'; id: string; name: string; input: unknown; result?: { ok: boolean; preview: string } }
  | { kind: 'error'; message: string }
> {
  const out: ReturnType<typeof steps> = []
  const byId = new Map<string, { kind: 'tool'; id: string; name: string; input: unknown; result?: { ok: boolean; preview: string } }>()
  for (const e of events) {
    if (e.kind === 'thinking' || e.kind === 'text') out.push({ kind: e.kind, text: e.text })
    else if (e.kind === 'tool_use') {
      const step = { kind: 'tool' as const, id: e.id, name: e.name, input: e.input }
      byId.set(e.id, step)
      out.push(step)
    } else if (e.kind === 'tool_result') {
      const step = byId.get(e.id)
      if (step) step.result = { ok: e.ok, preview: e.preview }
    } else if (e.kind === 'error') out.push({ kind: 'error', message: e.message })
  }
  return out
}

export function TraceView({
  trace,
  labels
}: {
  trace: EvalTrace
  labels?: Record<string, string>
}): JSX.Element {
  const [allOpen, setAllOpen] = useState(false)
  const items = steps(trace.events)
  const label = (id: string): string => labels?.[id] ?? id
  return (
    <div className="eval-trace">
      <div className="eval-trace-summary small">
        <span>{trace.turns} turn{trace.turns === 1 ? '' : 's'}</span>
        <span>{trace.seconds.toFixed(1)}s</span>
        {typeof trace.cost_usd === 'number' && <span>${trace.cost_usd.toFixed(4)}</span>}
        <span>model: {trace.model ?? 'default'}</span>
        <span className="spacer" />
        {items.length > 0 && (
          <button className="mini-btn" onClick={() => setAllOpen((o) => !o)}>
            {allOpen ? 'collapse all' : 'expand all'}
          </button>
        )}
      </div>

      {trace.error && (
        <div className="eval-trace-error">
          <strong>The run failed:</strong> {trace.error}
          {/not logged in|\/login|authentication|api key/i.test(trace.error) && (
            <div className="small">
              The agent could not authenticate. Start the explorer from your own terminal (
              <code>npm run web</code>) so it uses your Claude login, or set <code>ANTHROPIC_API_KEY</code> in{' '}
              <code>apps/graph-explorer/.env.local</code>.
            </div>
          )}
        </div>
      )}

      <ol className="eval-trace-steps">
        <li className="eval-trace-step user">
          <div className="eval-trace-role">question</div>
          <div>{trace.question}</div>
        </li>
        {items.map((st, i) => {
          if (st.kind === 'thinking')
            return (
              <li key={i} className="eval-trace-step thinking">
                <details open={allOpen}>
                  <summary className="eval-trace-role">thinking</summary>
                  <pre className="mono small">{st.text}</pre>
                </details>
              </li>
            )
          if (st.kind === 'text')
            return (
              <li key={i} className="eval-trace-step text">
                <div className="eval-trace-role">assistant</div>
                <div className="eval-trace-text">{st.text}</div>
              </li>
            )
          if (st.kind === 'error')
            return (
              <li key={i} className="eval-trace-step error">
                <div className="eval-trace-role">error</div>
                <div>{st.message}</div>
              </li>
            )
          if (st.kind !== 'tool') return null
          const status = st.result === undefined ? '…' : st.result.ok ? '✓' : '✗'
          return (
            <li key={i} className={`eval-trace-step tool ${st.result && !st.result.ok ? 'err' : ''}`}>
              <details open={allOpen || st.name === 'submit_answer'}>
                <summary>
                  <span className="eval-trace-role">tool</span> <span className="mono">{st.name}</span>{' '}
                  <span className="trace-status">{status}</span>
                </summary>
                <div className="eval-trace-io">
                  <div className="muted small">input</div>
                  <pre className="mono small">{pretty(st.input)}</pre>
                  {st.result && (
                    <>
                      <div className="muted small">result</div>
                      <pre className="mono small">{st.result.preview}</pre>
                    </>
                  )}
                </div>
              </details>
            </li>
          )
        })}
        {items.length === 0 && !trace.error && <li className="muted small">the agent took no steps</li>}
        <li className="eval-trace-step outcome">
          <div className="eval-trace-role">submitted</div>
          <div className="evals-id-chips">
            {trace.predicted_ids.map((id) => (
              <span key={id} className={`mini-btn ${trace.expected_ids.includes(id) ? 'evals-hit' : 'evals-miss'}`} title={id}>
                {label(id)}
              </span>
            ))}
            {trace.predicted_ids.length === 0 && <span className="muted small">(nothing — no answer was submitted)</span>}
          </div>
        </li>
      </ol>
    </div>
  )
}
