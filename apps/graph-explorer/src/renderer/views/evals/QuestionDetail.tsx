// Question detail (docs/EXPLORER_EVALS.md §5): the question, how far its expected
// answer can be trusted (critic confidence + notes) with a human verify/correct
// editor, the latest result (including why it failed), and the most recent full
// transcript of the agent answering it.
import { useEffect, useMemo, useState } from 'react'
import { useStore } from '../../store.js'
import { useEvalsStore } from '../../store-evals.js'
import { ConfidenceMeter } from './Confidence.js'
import { TraceView } from './TraceView.js'

interface Hit {
  id: string
  type: string
  label: string
}

function AnswerEditor({
  worldPath,
  qaId,
  initial,
  initialLabel,
  labels,
  onDone
}: {
  worldPath: string
  qaId: string
  initial: string[]
  initialLabel: string | null
  labels: Record<string, string>
  onDone: () => void
}): JSX.Element {
  const rpc = useStore((s) => s.rpc)
  const verifyQuestion = useEvalsStore((s) => s.verifyQuestion)
  const verifying = useEvalsStore((s) => s.verifying)
  const verifyError = useEvalsStore((s) => s.verifyError)
  const [ids, setIds] = useState<string[]>(initial)
  const [names, setNames] = useState<Record<string, string>>(labels)
  const [label, setLabel] = useState(initialLabel ?? '')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[]>([])

  useEffect(() => {
    if (!rpc || query.trim().length < 2) {
      setHits([])
      return
    }
    const t = setTimeout(() => {
      void rpc
        .call<Hit[]>('search', { worldPath, query, limit: 12 })
        .then(setHits)
        .catch(() => setHits([]))
    }, 200)
    return () => clearTimeout(t)
  }, [rpc, worldPath, query])

  const changed = ids.length !== initial.length || ids.some((id) => !initial.includes(id)) || label !== (initialLabel ?? '')

  return (
    <div className="answer-editor">
      <div className="evals-id-chips">
        {ids.map((id) => (
          <span key={id} className="mini-btn answer-chip" title={id}>
            {names[id] ?? id}
            <button className="answer-chip-x" aria-label={`remove ${names[id] ?? id}`} onClick={() => setIds(ids.filter((x) => x !== id))}>
              ×
            </button>
          </span>
        ))}
        {ids.length === 0 && <span className="muted small">(empty — add nodes below)</span>}
      </div>
      <input
        className="search-input small"
        placeholder="Search the graph to add a node to the answer…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {hits.length > 0 && (
        <div className="answer-hits">
          {hits
            .filter((h) => !ids.includes(h.id))
            .map((h) => (
              <button
                key={h.id}
                className="answer-hit"
                onClick={() => {
                  setIds([...ids, h.id])
                  setNames({ ...names, [h.id]: h.label })
                  setQuery('')
                }}
              >
                <span>{h.label}</span> <span className="muted small">{h.type}</span>
              </button>
            ))}
        </div>
      )}
      <label className="small muted">
        Answer label (optional, e.g. a count)
        <input className="search-input small" value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <div className="btn-row">
        <button
          className="btn primary small"
          disabled={verifying || ids.length === 0}
          onClick={async () => {
            const ok = await verifyQuestion(worldPath, qaId, changed ? ids : undefined, label !== (initialLabel ?? '') ? label : undefined)
            if (ok) onDone()
          }}
        >
          {verifying ? 'saving…' : changed ? 'Save correction (verified)' : 'Mark as verified'}
        </button>
        <button className="btn ghost small" onClick={onDone}>
          Cancel
        </button>
      </div>
      {verifyError && <div className="msg-error small">⚠ {verifyError}</div>}
    </div>
  )
}

export function QuestionDetail(): JSX.Element {
  const worldPath = useStore((s) => s.worldPath)
  const detailId = useEvalsStore((s) => s.detailId)
  const questions = useEvalsStore((s) => s.questions)
  const expectedLabels = useEvalsStore((s) => s.expectedLabels)
  const results = useEvalsStore((s) => s.results)
  const openInExplore = useEvalsStore((s) => s.openInExplore)
  const removeQuestions = useEvalsStore((s) => s.removeQuestions)
  const liveTraces = useEvalsStore((s) => s.liveTraces)
  const resultDetails = useEvalsStore((s) => s.resultDetails)
  const loadResultDetail = useEvalsStore((s) => s.loadResultDetail)
  const traces = useEvalsStore((s) => s.traces)
  const loadTrace = useEvalsStore((s) => s.loadTrace)
  const verifyQuestion = useEvalsStore((s) => s.verifyQuestion)
  const verifying = useEvalsStore((s) => s.verifying)
  const [editing, setEditing] = useState(false)

  const q = useMemo(() => questions.find((x) => x.id === detailId) ?? null, [questions, detailId])
  useEffect(() => setEditing(false), [detailId])

  // Most recent execution (any status) that answered this question.
  const latest = useMemo(() => {
    if (!q) return null
    for (const r of results) {
      if (r.report?.items.some((it) => it.qa_id === q.id)) return r
    }
    return null
  }, [results, q])

  useEffect(() => {
    if (worldPath && latest) void loadResultDetail(worldPath, latest.eval_id)
  }, [worldPath, latest, loadResultDetail])

  const detail = latest ? resultDetails[latest.eval_id] : undefined
  const item = q && detail ? detail.items.find((it) => it.qa_id === q.id) : undefined
  const scored = q && latest ? latest.report?.items.find((it) => it.qa_id === q.id) : undefined
  const hasTrace = Boolean(q && detail?.traced.includes(q.id))

  useEffect(() => {
    if (worldPath && latest && q && hasTrace) void loadTrace(worldPath, latest.eval_id, q.id)
  }, [worldPath, latest, q, hasTrace, loadTrace])

  if (!q) {
    return (
      <div className="evals-detail details-empty muted">
        Select a question to see how trustworthy its expected answer is, its latest result, and the full trace of the
        agent answering it.
      </div>
    )
  }

  const trace = latest ? traces[`${latest.eval_id}/${q.id}`] : undefined
  const labels = { ...expectedLabels, ...(detail?.labels ?? {}) }
  const live = liveTraces[q.id]
  const predicted = item?.predicted ?? scored?.predicted ?? []

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
        {q.verified && <span className="eval-badge ok">verified</span>}
      </div>

      <div className="block">
        <div className="section-title">Expected answer{q.expected_label ? ` (${q.expected_label})` : ''}</div>
        <div className="answer-confidence">
          <ConfidenceMeter q={q} showLabel />
          {q.critique && <div className="small muted answer-critique">{q.critique}</div>}
        </div>
        {editing && worldPath ? (
          <AnswerEditor
            worldPath={worldPath}
            qaId={q.id}
            initial={q.expected_ids}
            initialLabel={q.expected_label}
            labels={labels}
            onDone={() => setEditing(false)}
          />
        ) : (
          <>
            <div className="evals-id-chips">
              {q.expected_ids.map((id) => (
                <button key={id} className="mini-btn" onClick={() => openInExplore([id])} title={id}>
                  {labels[id] ?? id}
                </button>
              ))}
              {q.expected_ids.length === 0 && <span className="muted small">(empty set)</span>}
            </div>
            <div className="btn-row">
              {q.expected_ids.length > 0 && (
                <button className="mini-btn" onClick={() => openInExplore(q.expected_ids)}>
                  open full set in Explore
                </button>
              )}
              {worldPath && (
                <button className="mini-btn" onClick={() => setEditing(true)}>
                  edit answer…
                </button>
              )}
              {worldPath && !q.verified && (
                <button className="mini-btn" disabled={verifying} onClick={() => void verifyQuestion(worldPath, q.id)}>
                  looks right — mark verified
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {latest ? (
        <div className="block">
          <div className="section-title">
            Latest result — {latest.runner} · <span className="mono small">{latest.eval_id}</span>
          </div>
          {item?.error && (
            <div className="eval-trace-error">
              <strong>No answer — the run failed:</strong> {item.error}
            </div>
          )}
          <div className="evals-id-chips">
            {predicted.map((id) => (
              <button
                key={id}
                className={`mini-btn ${q.expected_ids.includes(id) ? 'evals-hit' : 'evals-miss'}`}
                onClick={() => openInExplore([id])}
                title={id}
              >
                {labels[id] ?? id}
              </button>
            ))}
            {predicted.length === 0 && !item?.error && (
              <span className="muted small">(the agent submitted nothing — see the trace below)</span>
            )}
          </div>
          {scored && (
            <table className="prop-table" style={{ marginTop: 8 }}>
              <tbody>
                <tr>
                  <td className="pk">F1 · P · R</td>
                  <td className="pv">
                    {scored.f1.toFixed(3)} · {scored.precision.toFixed(3)} · {scored.recall.toFixed(3)}
                  </td>
                </tr>
                <tr>
                  <td className="pk">exact match</td>
                  <td className="pv">{scored.exact_match ? 'yes' : 'no'}</td>
                </tr>
                {item && (
                  <tr>
                    <td className="pk">turns · time · cost</td>
                    <td className="pv">
                      {item.turns ?? '—'} · {item.seconds.toFixed(1)}s ·{' '}
                      {typeof item.cost_usd === 'number' ? `$${item.cost_usd.toFixed(4)}` : '—'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
          {item?.query && (
            <div className="final-query">
              <div className="final-query-head">
                <span className="engine-badge">engine: {item.engine ?? '—'}</span>
              </div>
              <pre className="final-query-code mono">{item.query}</pre>
            </div>
          )}
        </div>
      ) : (
        <div className="muted small">No execution has answered this question yet — press Run.</div>
      )}

      {latest && (
        <div className="block">
          <div className="section-title">Most recent trace</div>
          {trace === 'loading' && <div className="muted small">loading trace…</div>}
          {trace && trace !== 'loading' && <TraceView trace={trace} labels={labels} />}
          {detail && !hasTrace && (
            <div className="muted small">
              {latest.runner === 'explorer'
                ? 'This execution predates saved traces — run the question again to capture one.'
                : `The ${latest.runner} runner does not record a step-by-step trace; use the explorer runner for one.`}
            </div>
          )}
          {!detail && live && live.trace.length > 0 && (
            <div className="muted small">{live.trace.length} tool calls in the live run (full trace loads with the result)</div>
          )}
        </div>
      )}

      {q.source === 'proposed' && worldPath && (
        <button className="mini-btn" onClick={() => removeQuestions(worldPath, [q.id])}>
          remove from eval set
        </button>
      )}
    </div>
  )
}
