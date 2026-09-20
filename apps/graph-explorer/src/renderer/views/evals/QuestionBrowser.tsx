// The question browser (docs/EXPLORER_EVALS.md §5): search, group-by, counts,
// per-question row with last score + Run button, checkboxes for bulk selection.
import { useMemo } from 'react'
import { useStore } from '../../store.js'
import { useEvalsStore, type GroupBy } from '../../store-evals.js'
import type { QAPair } from '../../../shared/evals-protocol.js'

const GROUP_BYS: { id: GroupBy; label: string }[] = [
  { id: 'reasoning_type', label: 'Reasoning type' },
  { id: 'qtype', label: 'Question type' },
  { id: 'difficulty', label: 'Difficulty' },
  { id: 'source', label: 'Source' },
  { id: 'tag', label: 'Tag' }
]

function groupKeysFor(q: QAPair, groupBy: GroupBy): string[] {
  if (groupBy === 'tag') return q.tags.length ? q.tags : ['(untagged)']
  return [String((q as unknown as Record<string, unknown>)[groupBy] ?? '—')]
}

function matches(q: QAPair, search: string, labels: Record<string, string>): boolean {
  if (!search.trim()) return true
  const needle = search.trim().toLowerCase()
  if (q.question.toLowerCase().includes(needle)) return true
  if (q.id.toLowerCase().includes(needle)) return true
  return q.expected_ids.some((id) => (labels[id] ?? id).toLowerCase().includes(needle))
}

export function QuestionBrowser(): JSX.Element {
  const runPath = useStore((s) => s.runPath)
  const questions = useEvalsStore((s) => s.questions)
  const expectedLabels = useEvalsStore((s) => s.expectedLabels)
  const loading = useEvalsStore((s) => s.loadingQuestions)
  const search = useEvalsStore((s) => s.search)
  const setSearch = useEvalsStore((s) => s.setSearch)
  const groupBy = useEvalsStore((s) => s.groupBy)
  const setGroupBy = useEvalsStore((s) => s.setGroupBy)
  const selected = useEvalsStore((s) => s.selected)
  const toggleSelected = useEvalsStore((s) => s.toggleSelected)
  const selectAll = useEvalsStore((s) => s.selectAll)
  const clearSelected = useEvalsStore((s) => s.clearSelected)
  const detailId = useEvalsStore((s) => s.detailId)
  const setDetailId = useEvalsStore((s) => s.setDetailId)
  const latestScoreFor = useEvalsStore((s) => s.latestScoreFor)
  const runQuestions = useEvalsStore((s) => s.runQuestions)
  const activeRun = useEvalsStore((s) => s.activeRun)

  const filtered = useMemo(() => questions.filter((q) => matches(q, search, expectedLabels)), [questions, search, expectedLabels])

  const groups = useMemo(() => {
    const m = new Map<string, QAPair[]>()
    for (const q of filtered) {
      for (const key of groupKeysFor(q, groupBy)) {
        const arr = m.get(key)
        if (arr) arr.push(q)
        else m.set(key, [q])
      }
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [filtered, groupBy])

  const allVisible = filtered.map((q) => q.id)
  const allChecked = allVisible.length > 0 && allVisible.every((id) => selected.has(id))

  if (loading) return <div className="evals-browser panel-section muted">loading questions…</div>

  return (
    <div className="evals-browser">
      <div className="panel-section">
        <input
          className="search-input"
          placeholder="Search questions or answers…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="btn-row">
          <select className="run-select small" value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupBy)}>
            {GROUP_BYS.map((g) => (
              <option key={g.id} value={g.id}>
                group: {g.label}
              </option>
            ))}
          </select>
        </div>
        <div className="btn-row">
          <label className="evals-check-all">
            <input
              type="checkbox"
              checked={allChecked}
              onChange={() => (allChecked ? clearSelected() : selectAll(allVisible))}
            />
            <span className="small muted">
              {selected.size} selected · {filtered.length}/{questions.length} shown
            </span>
          </label>
        </div>
      </div>

      <div className="evals-question-list">
        {groups.map(([key, items]) => (
          <div key={key} className="evals-group">
            <div className="evals-group-head">
              <span className="mono small">{key}</span>
              <span className="evals-group-count small muted">{items.length}</span>
            </div>
            {items.map((q) => {
              const score = latestScoreFor(q.id)
              return (
                <div
                  key={q.id}
                  className={`evals-row ${detailId === q.id ? 'active' : ''}`}
                  onClick={() => setDetailId(q.id)}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(q.id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleSelected(q.id)}
                  />
                  <div className="evals-row-body">
                    <div className="evals-row-text">{q.question}</div>
                    <div className="evals-row-badges">
                      <span className="eval-badge muted">{q.reasoning_type}</span>
                      <span className="eval-badge muted">{q.qtype}</span>
                      <span className="eval-badge muted">{q.difficulty}</span>
                      {q.source === 'proposed' && <span className="eval-badge ok">proposed</span>}
                      {score && (
                        <span className={`eval-badge ${score.f1 >= 0.75 ? 'ok' : score.f1 > 0 ? '' : 'bad'}`}>
                          F1 {score.f1.toFixed(2)}
                        </span>
                      )}
                    </div>
                  </div>
                  <button
                    className="mini-btn"
                    disabled={!!activeRun || !runPath}
                    onClick={(e) => {
                      e.stopPropagation()
                      if (runPath) runQuestions(runPath, { kind: 'ids', ids: [q.id] })
                    }}
                  >
                    Run
                  </button>
                </div>
              )
            })}
          </div>
        ))}
        {filtered.length === 0 && <div className="muted small" style={{ padding: 12 }}>no questions match</div>}
      </div>
    </div>
  )
}
