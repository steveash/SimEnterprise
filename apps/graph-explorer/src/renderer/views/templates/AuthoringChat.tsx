// Center pane of the Templates view: the authoring chat (docs/EXPLORER_TEMPLATES.md
// §4) — a ChatPanel-alike transcript (text/thinking/tool trace with expandable
// inputs/results) plus a validation card for the latest `templates validate`
// report, model selector, Stop/Send.
import { useEffect, useRef, useState } from 'react'
import { useTemplatesStore, type TemplateChatMessage, type TemplateTraceEntry } from '../../store-templates.js'
import { MODELS } from '../../constants.js'
import { ValidationCard } from './ValidationCard.js'

function summarizeInput(name: string, input: unknown): string {
  if (!input || typeof input !== 'object') return ''
  const o = input as Record<string, unknown>
  if (typeof o.file_path === 'string') return o.file_path
  if (typeof o.command === 'string') return String(o.command).slice(0, 80)
  if (typeof o.pattern === 'string') return String(o.pattern)
  return Object.values(o).map(String).join(' ').slice(0, 60)
}

function TraceRow({ t }: { t: TemplateTraceEntry }): JSX.Element {
  const [open, setOpen] = useState(false)
  const status = t.ok === undefined ? '…' : t.ok ? '✓' : '✗'
  return (
    <div className={`trace-row ${t.ok === false ? 'err' : ''}`}>
      <div className="trace-head" onClick={() => setOpen((o) => !o)}>
        <span className="trace-status">{status}</span>
        <span className="trace-name mono">{t.name}</span>
        <span className="trace-arg mono">{summarizeInput(t.name, t.input)}</span>
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

function Message({ m }: { m: TemplateChatMessage }): JSX.Element {
  if (m.role === 'user') {
    return (
      <div className="msg user">
        <div className="msg-body">{m.text}</div>
      </div>
    )
  }
  return (
    <div className="msg assistant">
      {m.thinking && (
        <details className="block">
          <summary>thinking</summary>
          <div className="muted small">{m.thinking}</div>
        </details>
      )}
      {m.trace.length > 0 && (
        <div className="trace">
          {m.trace.map((t) => (
            <TraceRow key={t.id} t={t} />
          ))}
        </div>
      )}
      {m.text && <div className="msg-body">{m.text}</div>}
      {!m.done && !m.text && m.trace.length === 0 && <div className="muted small">thinking…</div>}
      {m.error && <div className="msg-error">⚠ {m.error}</div>}
    </div>
  )
}

export function AuthoringChat({ slug }: { slug: string }): JSX.Element {
  const chat = useTemplatesStore((s) => s.chatBySlug[slug])
  const validation = useTemplatesStore((s) => s.validationBySlug[slug] ?? null)
  const sendChat = useTemplatesStore((s) => s.sendChat)
  const cancelChat = useTemplatesStore((s) => s.cancelChat)
  const modelId = useTemplatesStore((s) => s.modelId)
  const setModelId = useTemplatesStore((s) => s.setModelId)
  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  const messages = chat?.messages ?? []
  const active = chat?.active ?? null

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const submit = () => {
    if (!draft.trim() || active) return
    sendChat(slug, draft)
    setDraft('')
  }

  return (
    <div className="chat tpl-chat">
      <ValidationCard report={validation} />
      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="chat-empty muted">
            Describe (or adjust) the <span className="mono">{slug}</span> template. The agent authors
            <span className="mono"> plugin.py</span> + tests under <span className="mono">templates/{slug}/</span>, running the
            validate loop until it's green.
          </div>
        )}
        {messages.map((m) => (
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
          placeholder="Ask the agent to author or adjust this template…"
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
          <button className="btn stop" onClick={() => cancelChat(slug)}>
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
