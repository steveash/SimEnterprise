// Propose tab (docs/EXPLORER_EVALS.md §4/§5): the proposal chat + a review list
// of every `propose_question` call (valid or rejected), with checkboxes and
// "Add to eval set".
import { useEffect, useRef, useState } from 'react'
import { useStore } from '../../store.js'
import { useEvalsStore, type ProposeMessage } from '../../store-evals.js'
import { MODELS } from '../../constants.js'

const EXAMPLES = [
  'Propose 5 more direct_relation questions about who reports to whom.',
  'Propose questions about goal_tree reasoning — what advances which goals.',
  'More like the aggregation ones, but about departments instead of teams.'
]

function Message({ m }: { m: ProposeMessage }): JSX.Element {
  if (m.role === 'user') {
    return (
      <div className="msg user">
        <div className="msg-body">{m.text}</div>
      </div>
    )
  }
  return (
    <div className="msg assistant">
      {m.text && <div className="msg-body">{m.text}</div>}
      {!m.done && !m.text && <div className="muted small">thinking…</div>}
      {m.error && <div className="msg-error">⚠ {m.error}</div>}
    </div>
  )
}

export function ProposeTab(): JSX.Element {
  const runPath = useStore((s) => s.runPath)
  const chat = useEvalsStore((s) => s.proposeChat)
  const active = useEvalsStore((s) => s.proposeActive)
  const send = useEvalsStore((s) => s.sendPropose)
  const cancel = useEvalsStore((s) => s.cancelPropose)
  const model = useEvalsStore((s) => s.proposeModel)
  const setModel = useEvalsStore((s) => s.setProposeModel)
  const reviewList = useEvalsStore((s) => s.reviewList)
  const toggleReviewChecked = useEvalsStore((s) => s.toggleReviewChecked)
  const clearReview = useEvalsStore((s) => s.clearReview)
  const addCheckedToSet = useEvalsStore((s) => s.addCheckedToSet)
  const addingToSet = useEvalsStore((s) => s.addingToSet)
  const openInExplore = useEvalsStore((s) => s.openInExplore)

  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [chat])

  const submit = () => {
    if (!draft.trim() || active || !runPath) return
    send(runPath, draft)
    setDraft('')
  }

  const checkedCount = reviewList.filter((r) => r.checked && r.valid).length

  return (
    <div className="evals-propose">
      <div className="evals-propose-chat chat">
        <div className="chat-scroll" ref={scrollRef}>
          {chat.length === 0 && (
            <div className="chat-empty">
              <div className="muted">
                Tell the agent what kind of questions you want; every answer it proposes is derived from a
                real query against this run's graph.
              </div>
              <div className="examples">
                {EXAMPLES.map((ex) => (
                  <button key={ex} className="example-chip" onClick={() => runPath && send(runPath, ex)}>
                    {ex}
                  </button>
                ))}
              </div>
            </div>
          )}
          {chat.map((m) => (
            <Message key={m.id} m={m} />
          ))}
        </div>
        <div className="chat-input">
          <select className="model-select" value={model} onChange={(e) => setModel(e.target.value)}>
            {MODELS.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
          <textarea
            className="chat-textarea"
            placeholder="What kind of questions do you want?"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
          />
          {active ? (
            <button className="btn stop" onClick={cancel}>
              Stop
            </button>
          ) : (
            <button className="btn primary" onClick={submit} disabled={!draft.trim() || !runPath}>
              Send
            </button>
          )}
        </div>
      </div>

      <div className="evals-propose-review panel-section">
        <div className="section-title">
          Review ({reviewList.length}) — {checkedCount} to add
        </div>
        <div className="evals-review-list">
          {reviewList.map((r) => (
            <div key={r.key} className={`evals-review-row ${r.valid ? '' : 'evals-review-invalid'}`}>
              <input
                type="checkbox"
                checked={r.checked}
                disabled={!r.valid}
                onChange={() => toggleReviewChecked(r.key)}
              />
              <div className="evals-row-body">
                <div className="evals-row-text">{r.proposal.question}</div>
                <div className="evals-row-badges">
                  <span className="eval-badge muted">{r.proposal.reasoning_type || '—'}</span>
                  <span className="eval-badge muted">{r.proposal.difficulty}</span>
                  {!r.valid && <span className="eval-badge bad">{r.reason}</span>}
                </div>
                {r.proposal.rationale && <div className="small muted">{r.proposal.rationale}</div>}
                {r.proposal.expected_ids.length > 0 && (
                  <div className="evals-id-chips">
                    {r.proposal.expected_ids.map((id) => (
                      <button key={id} className="mini-btn" onClick={() => openInExplore([id])} title={id}>
                        {id}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
          {reviewList.length === 0 && <div className="muted small">Proposals the agent makes will appear here.</div>}
        </div>
        <div className="btn-row">
          <button
            className="btn primary"
            disabled={checkedCount === 0 || addingToSet || !runPath}
            onClick={() => runPath && addCheckedToSet(runPath)}
          >
            Add {checkedCount} to eval set
          </button>
          <button className="btn ghost" disabled={reviewList.length === 0} onClick={clearReview}>
            Clear
          </button>
        </div>
      </div>
    </div>
  )
}
