// Run controls (docs/EXPLORER_EVALS.md §5): run selected / run N% (stratified,
// seeded) / run all; runner + model + concurrency; a live progress bar +
// macro-F1-so-far; cancel.
import { useStore } from '../../store.js'
import { useEvalsStore } from '../../store-evals.js'
import { MODELS } from '../../constants.js'
import type { EvalRunner } from '../../../shared/evals-protocol.js'

const RUNNERS: { id: EvalRunner; label: string; title: string }[] = [
  { id: 'explorer', label: 'Explorer', title: 'answer in-process, in this app' },
  { id: 'rag', label: 'RAG', title: 'the Python RAG baseline runner (enterprise-sim evals run --runner rag)' },
  { id: 'graph', label: 'Graph', title: 'the Python graph-agent runner (enterprise-sim evals run --runner graph)' }
]

export function RunControls(): JSX.Element {
  const runPath = useStore((s) => s.runPath)
  const questions = useEvalsStore((s) => s.questions)
  const selected = useEvalsStore((s) => s.selected)
  const runner = useEvalsStore((s) => s.runner)
  const setRunner = useEvalsStore((s) => s.setRunner)
  const model = useEvalsStore((s) => s.model)
  const setModel = useEvalsStore((s) => s.setModel)
  const concurrency = useEvalsStore((s) => s.concurrency)
  const setConcurrency = useEvalsStore((s) => s.setConcurrency)
  const fraction = useEvalsStore((s) => s.sampleFraction)
  const setSampleFraction = useEvalsStore((s) => s.setSampleFraction)
  const seed = useEvalsStore((s) => s.sampleSeed)
  const setSampleSeed = useEvalsStore((s) => s.setSampleSeed)
  const stratify = useEvalsStore((s) => s.sampleStratify)
  const setSampleStratify = useEvalsStore((s) => s.setSampleStratify)
  const activeRun = useEvalsStore((s) => s.activeRun)
  const runProgress = useEvalsStore((s) => s.runProgress)
  const runError = useEvalsStore((s) => s.runError)
  const runQuestions = useEvalsStore((s) => s.runQuestions)
  const cancelRun = useEvalsStore((s) => s.cancelRun)

  const disabled = !runPath || !!activeRun || questions.length === 0
  const sampleCount = Math.max(0, Math.ceil((fraction / 100) * questions.length))

  return (
    <div className="evals-run-controls panel-section">
      <div className="btn-row">
        <button
          className="btn"
          disabled={disabled || selected.size === 0}
          onClick={() => runPath && runQuestions(runPath, { kind: 'ids', ids: [...selected] })}
        >
          Run selected ({selected.size})
        </button>
        <button
          className="btn"
          disabled={disabled}
          onClick={() =>
            runPath &&
            runQuestions(runPath, { kind: 'fraction', fraction: fraction / 100, seed, stratify })
          }
        >
          Run {fraction}% (~{sampleCount})
        </button>
        <button className="btn" disabled={disabled} onClick={() => runPath && runQuestions(runPath, { kind: 'all' })}>
          Run all ({questions.length})
        </button>
        {activeRun && (
          <button className="btn stop" onClick={cancelRun}>
            Cancel
          </button>
        )}
      </div>

      <div className="btn-row">
        <div className="engine-tabs">
          {RUNNERS.map((r) => (
            <button key={r.id} title={r.title} className={runner === r.id ? 'active' : ''} onClick={() => setRunner(r.id)}>
              {r.label}
            </button>
          ))}
        </div>
        <select className="model-select" value={model} onChange={(e) => setModel(e.target.value)}>
          {MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
        <label className="small muted">
          concurrency
          <input
            className="depth-input"
            type="number"
            min={1}
            max={4}
            value={concurrency}
            onChange={(e) => setConcurrency(Number(e.target.value))}
          />
        </label>
        <label className="small muted">
          %
          <input
            className="depth-input"
            type="number"
            min={1}
            max={100}
            value={fraction}
            onChange={(e) => setSampleFraction(Number(e.target.value))}
          />
        </label>
        <label className="small muted">
          seed
          <input className="depth-input" type="number" value={seed} onChange={(e) => setSampleSeed(Number(e.target.value))} />
        </label>
        <label className="small muted evals-stratify">
          <input type="checkbox" checked={stratify} onChange={(e) => setSampleStratify(e.target.checked)} />
          stratify
        </label>
      </div>

      {runProgress && (
        <div className="evals-progress">
          <div className="evals-progress-bar">
            <div
              className="evals-progress-fill"
              style={{ width: `${runProgress.total ? (100 * runProgress.done) / runProgress.total : 0}%` }}
            />
          </div>
          <div className="small muted">
            {runProgress.done}/{runProgress.total} · macro-F1 so far {runProgress.macroF1SoFar.toFixed(3)}
          </div>
        </div>
      )}
      {runError && <div className="msg-error small">⚠ {runError}</div>}
    </div>
  )
}
