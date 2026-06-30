import { useEffect, useRef, useState } from 'react'
import { useStore, type ChatMessage, type TraceEntry } from '../store.js'
import { MODELS } from '../constants.js'

const EXAMPLES = [
  'Who does the Senior Engineer report to, all the way up the chain?',
  'Which people are in the Engineering department? Use reasoning.',
  'What initiatives advance Goal 2, directly or via subgoals?',
  'How is Ben Cho connected to the Kickoff Brief artifact?',
  'Show everyone who collaborates with someone on the build-software project.'
]

export function ChatPanel(): JSX.Element {
  const chat = useStore((s) => s.chat)
  const chatActive = useStore((s) => s.chatActive)
  const sendChat = useStore((s) => s.sendChat)
  const cancelChat = useStore((s) => s.cancelChat)
  const modelId = useStore((s) => s.model_id)
  const setModelId = useStore((s) => s.setModelId)
  const hasApiKey = useStore((s) => s.hasApiKey)
  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [chat])

  const submit = () => {
    if (!draft.trim() || chatActive) return
    sendChat(draft)
    setDraft('')
  }

  return (
    <div className="chat">
      {!hasApiKey && <ApiKeyBanner />}
      <div className="chat-scroll" ref={scrollRef}>
        {chat.length === 0 && (
          <div className="chat-empty">
            <div className="muted">Ask the agent about this graph. It will pick Cypher or SPARQL, run the query, and highlight the answer.</div>
            <div className="examples">
              {EXAMPLES.map((ex) => (
                <button key={ex} className="example-chip" onClick={() => sendChat(ex)}>
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
        <select className="model-select" value={modelId} onChange={(e) => setModelId(e.target.value)}>
          {MODELS.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
        <textarea
          className="chat-textarea"
          placeholder="Ask about the graph…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submit()
            }
          }}
        />
        {chatActive ? (
          <button className="btn stop" onClick={cancelChat}>
            Stop
          </button>
        ) : (
          <button className="btn primary" onClick={submit} disabled={!draft.trim()}>
            Send
          </button>
        )}
      </div>
    </div>
  )
}

function Message({ m }: { m: ChatMessage }): JSX.Element {
  if (m.role === 'user') {
    return (
      <div className="msg user">
        <div className="msg-body">{m.text}</div>
      </div>
    )
  }
  return (
    <div className="msg assistant">
      {m.trace.length > 0 && (
        <div className="trace">
          {m.trace.map((t) => (
            <TraceRow key={t.id} t={t} />
          ))}
        </div>
      )}
      {m.text && <div className="msg-body">{m.text}</div>}
      {m.finalQuery && <FinalQueryBlock fq={m.finalQuery} />}
      {!m.done && !m.text && m.trace.length === 0 && <div className="muted small">thinking…</div>}
      {m.error && <div className="msg-error">⚠ {m.error}</div>}
    </div>
  )
}

/**
 * The structured "answer query" block: which engine the agent chose and the
 * exact final query that produced the answer, in a copyable code block. Distinct
 * from the live tool-trace above — this is the single query that mattered.
 */
function FinalQueryBlock({ fq }: { fq: NonNullable<ChatMessage['finalQuery']> }): JSX.Element {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(fq.query)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }
  return (
    <div className="final-query">
      <div className="final-query-head">
        <span className="engine-badge">engine: {fq.engine}</span>
        <button className="copy-btn" onClick={copy}>
          {copied ? 'copied ✓' : 'copy'}
        </button>
      </div>
      <pre className="final-query-code mono">{fq.query}</pre>
    </div>
  )
}

function TraceRow({ t }: { t: TraceEntry }): JSX.Element {
  const [open, setOpen] = useState(false)
  const status = t.ok === undefined ? '…' : t.ok ? '✓' : '✗'
  const arg = summarizeInput(t.name, t.input)
  return (
    <div className={`trace-row ${t.ok === false ? 'err' : ''}`}>
      <div className="trace-head" onClick={() => setOpen((o) => !o)}>
        <span className="trace-status">{status}</span>
        <span className="trace-name mono">{t.name}</span>
        <span className="trace-arg mono">{arg}</span>
      </div>
      {open && (
        <div className="trace-detail">
          <div className="trace-sub">input</div>
          <pre className="mono">{JSON.stringify(t.input, null, 2)}</pre>
          {t.preview && (
            <>
              <div className="trace-sub">result</div>
              <pre className="mono">{t.preview}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function summarizeInput(name: string, input: unknown): string {
  if (!input || typeof input !== 'object') return ''
  const o = input as Record<string, unknown>
  if (name === 'cypher_query' || name === 'sparql_query') return String(o.query ?? '').slice(0, 80)
  if (o.query) return String(o.query)
  if (o.id) return String(o.id)
  if (o.from && o.to) return `${o.from} → ${o.to}`
  return Object.values(o).map(String).join(' ').slice(0, 60)
}

function ApiKeyBanner(): JSX.Element {
  const setApiKey = useStore((s) => s.setApiKey)
  const [key, setKey] = useState('')
  const [saving, setSaving] = useState(false)
  return (
    <div className="api-banner">
      <span className="small">No ANTHROPIC_API_KEY found. Paste one to enable the agent:</span>
      <input
        className="search-input small"
        type="password"
        placeholder="sk-ant-…"
        value={key}
        onChange={(e) => setKey(e.target.value)}
      />
      <button
        className="btn primary"
        disabled={!key.trim() || saving}
        onClick={async () => {
          setSaving(true)
          await setApiKey(key.trim())
          setSaving(false)
        }}
      >
        {saving ? 'restarting…' : 'Save'}
      </button>
    </div>
  )
}
