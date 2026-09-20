// Question detail (docs/EXPLORER_EVALS.md §5): question, expected answer (ids
// resolved to labels, clickable -> Explore), the latest prediction with
// per-item P/R/F1/EM, the agent's final engine + query.
import { useMemo } from 'react'
import { useStore } from '../../store.js'
import { useEvalsStore } from '../../store-evals.js'

export function QuestionDetail(): JSX.Element {
  const runPath = useStore((s) => s.runPath)
  const detailId = useEvalsStore((s) => s.detailId)
  const questions = useEvalsStore((s) => s.questions)
  const expectedLabels = useEvalsStore((s) => s.expectedLabels)
  const results = useEvalsStore((s) => s.results)
  const openInExplore = useEvalsStore((s) => s.openInExplore)
  const removeQuestions = useEvalsStore((s) => s.removeQuestions)
  const liveTraces = useEvalsStore((s) => s.liveTraces)

  const q = useMemo(() => questions.find((x) => x.id === detailId) ?? null, [questions, detailId])

  // Most recent execution (any status) that includes this question, for the trace/final-query.
  const latest = useMemo(() => {
    if (!q) return null
    for (const r of results) {
      const item = r.report?.items.find((it) => it.qa_id === q.id)
      if (item) return { execMeta: r, item }
    }
    return null
  }, [results, q])

  const live = q ? liveTraces[q.id] : undefined
  // The live trace's engine/query (from running this one question just now) wins
  // over a possibly-stale historical run; items.jsonl doesn't persist the raw
  // trace, so once the page reloads only engine/query survive (via the report).
  const engine = live?.engine
  const finalQuery = live?.query

  if (!q) {
    return (
      <div className="evals-detail details-empty muted">
        Select a question to see its expected answer, latest prediction and query.
      </div>
    )
  }

  return (
    <div className="evals-detail details">
      <div className="details-head">
        <div className="details-title">{q.question}</div>
      </div>
      <div className="details-id mono">{q.id}</div>
      <div className="evals-row-badges" style={{ margin: '8px 0' }}>
        <span className="eval-badge muted">{q.reasoning_type}</span>
        <span className="eval-badge muted">{q.qtype}</span>
        <span className="eval-badge muted">{q.difficulty}</span>
        <span className="eval-badge muted">{q.source}</span>
      </div>

      <div className="block">
        <div className="section-title">Expected answer{q.expected_label ? ` (${q.expected_label})` : ''}</div>
        <div className="evals-id-chips">
          {q.expected_ids.map((id) => (
            <button key={id} className="mini-btn" onClick={() => openInExplore([id])} title={id}>
              {expectedLabels[id] ?? id}
            </button>
          ))}
          {q.expected_ids.length === 0 && <span className="muted small">(empty set)</span>}
        </div>
        {q.expected_ids.length > 0 && (
          <button className="mini-btn" style={{ marginTop: 6 }} onClick={() => openInExplore(q.expected_ids)}>
            open full set in Explore
          </button>
        )}
      </div>

      {latest && (
        <div className="block">
          <div className="section-title">
            Latest prediction — {latest.execMeta.runner} · {latest.execMeta.eval_id}
          </div>
          <div className="evals-id-chips">
            {latest.item.predicted.map((id) => (
              <button
                key={id}
                className={`mini-btn ${q.expected_ids.includes(id) ? 'evals-hit' : 'evals-miss'}`}
                onClick={() => openInExplore([id])}
                title={id}
              >
                {expectedLabels[id] ?? id}
              </button>
            ))}
            {latest.item.predicted.length === 0 && <span className="muted small">(no prediction)</span>}
          </div>
          <table className="prop-table" style={{ marginTop: 8 }}>
            <tbody>
              <tr>
                <td className="pk">precision</td>
                <td className="pv">{latest.item.precision.toFixed(3)}</td>
              </tr>
              <tr>
                <td className="pk">recall</td>
                <td className="pv">{latest.item.recall.toFixed(3)}</td>
              </tr>
              <tr>
                <td className="pk">F1</td>
                <td className="pv">{latest.item.f1.toFixed(3)}</td>
              </tr>
              <tr>
                <td className="pk">exact match</td>
                <td className="pv">{latest.item.exact_match ? 'yes' : 'no'}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {!latest && <div className="muted small">No execution has answered this question yet.</div>}

      {(engine || finalQuery) && (
        <div className="final-query">
          <div className="final-query-head">
            <span className="engine-badge">engine: {engine ?? '—'}</span>
          </div>
          {finalQuery && <pre className="final-query-code mono">{finalQuery}</pre>}
        </div>
      )}

      {live && live.trace.length > 0 && (
        <div className="block">
          <div className="section-title">Tool trace (last single-question run)</div>
          <div className="trace">
            {live.trace.map((t) => (
              <div key={t.id} className={`trace-row ${t.ok === false ? 'err' : ''}`}>
                <div className="trace-head">
                  <span className="trace-status">{t.ok === undefined ? '…' : t.ok ? '✓' : '✗'}</span>
                  <span className="trace-name mono">{t.name}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {q.source === 'proposed' && runPath && (
        <button className="mini-btn" onClick={() => removeQuestions(runPath, [q.id])}>
          remove from eval set
        </button>
      )}
    </div>
  )
}
