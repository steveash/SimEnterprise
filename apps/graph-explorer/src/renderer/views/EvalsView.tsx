// The Evals view (docs/EXPLORER_EVALS.md): browse a run's eval question set,
// run one / a % / all with a live accuracy score, keep the history of
// executions, and propose new questions with an AI chat.
import { useEffect, useState } from 'react'
import { useStore } from '../store.js'
import { useEvalsStore } from '../store-evals.js'
import { QuestionsTab } from './evals/QuestionsTab.js'
import { ExecutionsTab } from './evals/ExecutionsTab.js'
import { ProposeTab } from './evals/ProposeTab.js'
import './evals.css'

type EvalsTab = 'questions' | 'executions' | 'propose'

const TABS: { id: EvalsTab; label: string }[] = [
  { id: 'questions', label: 'Questions' },
  { id: 'executions', label: 'Executions' },
  { id: 'propose', label: 'Propose' }
]

export function EvalsView(): JSX.Element {
  const runPath = useStore((s) => s.runPath)
  const hasQuestionSet = useEvalsStore((s) => s.hasQuestionSet)
  const loading = useEvalsStore((s) => s.loadingQuestions)
  const loadError = useEvalsStore((s) => s.loadError)
  const loadQuestions = useEvalsStore((s) => s.loadQuestions)
  const generateQuestions = useEvalsStore((s) => s.generateQuestions)
  const [tab, setTab] = useState<EvalsTab>('questions')

  useEffect(() => {
    if (runPath) void loadQuestions(runPath)
  }, [runPath, loadQuestions])

  if (!runPath) {
    return (
      <div className="view view-evals">
        <div className="view-empty">
          <div className="view-empty-title">Evals</div>
          <div className="muted">Load a run first.</div>
        </div>
      </div>
    )
  }

  if (!loading && !hasQuestionSet) {
    return (
      <div className="view view-evals">
        <div className="view-empty">
          <div className="view-empty-title">No eval question set yet</div>
          <div className="muted" style={{ marginBottom: 12 }}>
            This run has no <span className="mono">evals/questions.jsonl</span>. Derive one from its gold KG.
          </div>
          {loadError && <div className="msg-error small">⚠ {loadError}</div>}
          <button className="btn primary" onClick={() => void generateQuestions(runPath)}>
            Generate question set
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="view view-evals evals">
      <div className="tabs evals-tabs">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="evals-tab-body">
        {tab === 'questions' && <QuestionsTab />}
        {tab === 'executions' && <ExecutionsTab />}
        {tab === 'propose' && <ProposeTab />}
      </div>
    </div>
  )
}
