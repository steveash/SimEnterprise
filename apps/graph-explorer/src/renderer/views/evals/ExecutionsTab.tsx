// Executions tab (docs/EXPLORER_EVALS.md §5): history of `evals/results/*` with
// report tables (overall + by reasoning type), open/compare, the predictions path.
import { useEffect } from 'react'
import { useStore } from '../../store.js'
import { useEvalsStore } from '../../store-evals.js'
import type { Aggregate } from '../../../shared/evals-protocol.js'

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

export function ExecutionsTab(): JSX.Element {
  const runPath = useStore((s) => s.runPath)
  const results = useEvalsStore((s) => s.results)
  const loading = useEvalsStore((s) => s.loadingResults)
  const loadResults = useEvalsStore((s) => s.loadResults)
  const selectedId = useEvalsStore((s) => s.selectedResultId)
  const selectResult = useEvalsStore((s) => s.selectResult)

  useEffect(() => {
    if (runPath) void loadResults(runPath)
  }, [runPath, loadResults])

  const selected = results.find((r) => r.eval_id === selectedId) ?? null

  return (
    <div className="evals-executions">
      <div className="evals-exec-list panel-section">
        <div className="section-title">Executions {loading && <span className="muted">(loading…)</span>}</div>
        <table className="result-table">
          <thead>
            <tr>
              <th>eval id</th>
              <th>runner</th>
              <th>model</th>
              <th>status</th>
              <th>n</th>
              <th>F1</th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr key={r.eval_id} className="diff-row" onClick={() => selectResult(r.eval_id)}>
                <td className="mono">{r.eval_id}</td>
                <td>{r.runner}</td>
                <td>{r.model ?? '—'}</td>
                <td>{r.status}</td>
                <td>{r.report?.overall.count ?? r.question_ids.length}</td>
                <td>{r.report ? r.report.overall.macro_f1.toFixed(3) : '—'}</td>
              </tr>
            ))}
            {results.length === 0 && !loading && (
              <tr>
                <td colSpan={6} className="muted">
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
          <div className="small mono" style={{ marginTop: 8 }}>
            predictions: {runPath ? `${runPath}/evals/results/${selected.eval_id}/predictions.jsonl` : ''}
          </div>
        </div>
      )}
    </div>
  )
}
