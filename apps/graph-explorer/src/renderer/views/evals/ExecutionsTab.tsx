// Executions tab (docs/EXPLORER_EVALS.md §5): every `evals/results/*` execution;
// drill into one to see its report and every question it answered (score,
// errors, cost), then into any question for expected vs submitted, the final
// query, and the full transcript of the agent answering it.
import { useEffect, useMemo, useState } from 'react'
import { useStore } from '../../store.js'
import { useEvalsStore } from '../../store-evals.js'
import type { Aggregate, EvalItemResult } from '../../../shared/evals-protocol.js'
import { TraceView } from './TraceView.js'

function AggRow({ label, agg }: { label: string; agg: Aggregate }): JSX.Element {
  return (
    <tr>
      <td className="pk mono">{label}</td>
      <td className="pv">{agg.count}</td>
      <td className="pv">{agg.macro_f1.toFixed(3)}</td>
      <td className="pv">{agg.macro_precision.toFixed(3)}</td>
      <td className="pv">{agg.macro_recall.toFixed(3)}</td>
      <td className="pv">{agg.exact_match_rate.toFixed(3)}</td>
    </tr>
  )
}

type ItemFilter = 'all' | 'failed' | 'misses'

function ItemDetail({ evalId, item, labels, traced }: { evalId: string; item: EvalItemResult; labels: Record<string, string>; traced: boolean }): JSX.Element {
  const worldPath = useStore((s) => s.worldPath)
  const traces = useEvalsStore((s) => s.traces)
  const loadTrace = useEvalsStore((s) => s.loadTrace)
  const openInExplore = useEvalsStore((s) => s.openInExplore)
  const setDetailId = useEvalsStore((s) => s.setDetailId)

  useEffect(() => {
    if (worldPath && traced) void loadTrace(worldPath, evalId, item.qa_id)
  }, [worldPath, evalId, item.qa_id, traced, loadTrace])

  const trace = traces[`${evalId}/${item.qa_id}`]
  const label = (id: string): string => labels[id] ?? id
  const missed = item.expected.filter((id) => !item.predicted.includes(id))

  return (
    <div className="evals-exec-item panel-section">
      <div className="section-title">{item.question}</div>
      <div className="details-id mono">{item.qa_id}</div>
      <div className="evals-row-badges" style={{ margin: '6px 0' }}>
        <span className="eval-badge muted">{item.reasoning_type}</span>
        <span className={`eval-badge ${item.f1 >= 0.75 ? 'ok' : item.f1 > 0 ? '' : 'bad'}`}>F1 {item.f1.toFixed(2)}</span>
        {item.exact_match && <span className="eval-badge ok">exact</span>}
        {item.error && <span className="eval-badge bad">error</span>}
      </div>

      {item.error && (
        <div className="eval-trace-error">
          <strong>The run failed:</strong> {item.error}
        </div>
      )}

      <div className="block">
        <div className="section-title">Expected ({item.expected.length})</div>
        <div className="evals-id-chips">
          {item.expected.map((id) => (
            <button
              key={id}
              className={`mini-btn ${item.predicted.includes(id) ? 'evals-hit' : ''}`}
              onClick={() => openInExplore([id])}
              title={id}
            >
              {label(id)}
            </button>
          ))}
        </div>
        <div className="section-title" style={{ marginTop: 8 }}>
          Submitted ({item.predicted.length}){missed.length > 0 ? ` · missed ${missed.length}` : ''}
        </div>
        <div className="evals-id-chips">
          {item.predicted.map((id) => (
            <button
              key={id}
              className={`mini-btn ${item.expected.includes(id) ? 'evals-hit' : 'evals-miss'}`}
              onClick={() => openInExplore([id])}
              title={id}
            >
              {label(id)}
            </button>
          ))}
          {item.predicted.length === 0 && <span className="muted small">(nothing submitted)</span>}
        </div>
        <div className="small muted" style={{ marginTop: 6 }}>
          P {item.precision.toFixed(3)} · R {item.recall.toFixed(3)} · {item.turns ?? '—'} turns · {item.seconds.toFixed(1)}s
          {typeof item.cost_usd === 'number' ? ` · $${item.cost_usd.toFixed(4)}` : ''}
        </div>
        <button className="mini-btn" style={{ marginTop: 6 }} onClick={() => setDetailId(item.qa_id)} title="select this question on the Questions tab">
          open question
        </button>
      </div>

      {item.query && (
        <div className="final-query">
          <div className="final-query-head">
            <span className="engine-badge">engine: {item.engine ?? '—'}</span>
          </div>
          <pre className="final-query-code mono">{item.query}</pre>
        </div>
      )}

      <div className="block">
        <div className="section-title">Trace</div>
        {!traced && <div className="muted small">No step-by-step trace was recorded for this answer.</div>}
        {trace === 'loading' && <div className="muted small">loading trace…</div>}
        {trace && trace !== 'loading' && <TraceView trace={trace} labels={labels} />}
      </div>
    </div>
  )
}

export function ExecutionsTab(): JSX.Element {
  const worldPath = useStore((s) => s.worldPath)
  const results = useEvalsStore((s) => s.results)
  const loading = useEvalsStore((s) => s.loadingResults)
  const loadResults = useEvalsStore((s) => s.loadResults)
  const selectedId = useEvalsStore((s) => s.selectedResultId)
  const selectResult = useEvalsStore((s) => s.selectResult)
  const resultDetails = useEvalsStore((s) => s.resultDetails)
  const loadResultDetail = useEvalsStore((s) => s.loadResultDetail)
  const selectedItem = useEvalsStore((s) => s.selectedExecItem)
  const selectExecItem = useEvalsStore((s) => s.selectExecItem)
  const [filter, setFilter] = useState<ItemFilter>('all')

  useEffect(() => {
    if (worldPath) void loadResults(worldPath)
  }, [worldPath, loadResults])

  const selected = results.find((r) => r.eval_id === selectedId) ?? null
  useEffect(() => {
    if (worldPath && selected) void loadResultDetail(worldPath, selected.eval_id)
  }, [worldPath, selected, loadResultDetail])

  const detail = selected ? resultDetails[selected.eval_id] : undefined
  const items = useMemo(() => {
    const all = detail?.items ?? []
    if (filter === 'failed') return all.filter((it) => it.error)
    if (filter === 'misses') return all.filter((it) => !it.exact_match)
    return all
  }, [detail, filter])
  const item = detail?.items.find((it) => it.qa_id === selectedItem) ?? null
  const failed = detail?.items.filter((it) => it.error).length ?? 0

  return (
    <div className={`evals-executions ${selected ? 'drilled' : ''}`}>
      <div className="evals-exec-list panel-section">
        <div className="section-title">Executions {loading && <span className="muted">(loading…)</span>}</div>
        <table className="result-table">
          <thead>
            <tr>
              <th>eval id</th>
              <th>runner</th>
              <th>status</th>
              <th>n</th>
              <th>F1</th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr
                key={r.eval_id}
                className={`diff-row ${r.eval_id === selectedId ? 'active' : ''}`}
                onClick={() => selectResult(r.eval_id)}
              >
                <td className="mono">{r.eval_id}</td>
                <td>
                  {r.runner}
                  {r.model ? <span className="muted small"> · {r.model}</span> : null}
                </td>
                <td>{r.status}</td>
                <td>{r.report?.overall.count ?? r.question_ids.length}</td>
                <td>{r.report ? r.report.overall.macro_f1.toFixed(3) : '—'}</td>
              </tr>
            ))}
            {results.length === 0 && !loading && (
              <tr>
                <td colSpan={5} className="muted">
                  no executions yet — run some questions from the Questions tab
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="evals-exec-detail panel-section">
          <div className="section-title">
            {selected.eval_id} — {selected.runner}
            {selected.model ? ` · ${selected.model}` : ''}
          </div>
          <div className="small muted">
            started {selected.started_at} {selected.finished_at ? `· finished ${selected.finished_at}` : ''}
            {typeof selected.cost_usd === 'number' ? ` · $${selected.cost_usd.toFixed(4)}` : ''}
          </div>
          {failed > 0 && (
            <div className="eval-trace-error small" style={{ marginTop: 6 }}>
              {failed} question{failed === 1 ? '' : 's'} failed to run — filter to “failed” to see why.
            </div>
          )}
          {selected.report && (
            <table className="result-table" style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>reasoning type</th>
                  <th>n</th>
                  <th>F1</th>
                  <th>P</th>
                  <th>R</th>
                  <th>EM</th>
                </tr>
              </thead>
              <tbody>
                <AggRow label="overall" agg={selected.report.overall} />
                {Object.entries(selected.report.by_reasoning_type).map(([t, agg]) => (
                  <AggRow key={t} label={t} agg={agg} />
                ))}
              </tbody>
            </table>
          )}

          <div className="btn-row" style={{ marginTop: 10 }}>
            <div className="section-title" style={{ margin: 0 }}>
              Questions ({detail?.items.length ?? '…'})
            </div>
            <span className="spacer" />
            <div className="tabs">
              {(['all', 'failed', 'misses'] as ItemFilter[]).map((f) => (
                <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>
                  {f}
                </button>
              ))}
            </div>
          </div>
          {!detail && <div className="muted small">loading…</div>}
          {detail && (
            <table className="result-table exec-items">
              <thead>
                <tr>
                  <th>question</th>
                  <th>F1</th>
                  <th>EM</th>
                  <th>turns</th>
                  <th>s</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <tr
                    key={it.qa_id}
                    className={`diff-row ${it.qa_id === selectedItem ? 'active' : ''}`}
                    onClick={() => selectExecItem(it.qa_id)}
                  >
                    <td>
                      {it.error && <span className="eval-badge bad">error</span>} {it.question}
                    </td>
                    <td>{it.f1.toFixed(2)}</td>
                    <td>{it.exact_match ? '✓' : ''}</td>
                    <td>{it.turns ?? '—'}</td>
                    <td>{it.seconds.toFixed(1)}</td>
                  </tr>
                ))}
                {items.length === 0 && (
                  <tr>
                    <td colSpan={5} className="muted">
                      nothing matches this filter
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
          <div className="small mono" style={{ marginTop: 8 }}>
            predictions: {detail?.predictionsPath ?? ''}
          </div>
        </div>
      )}

      {selected && item && detail && (
        <ItemDetail evalId={selected.eval_id} item={item} labels={detail.labels} traced={detail.traced.includes(item.qa_id)} />
      )}
    </div>
  )
}
