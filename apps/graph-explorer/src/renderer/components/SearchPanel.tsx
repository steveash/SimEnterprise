import { useState } from 'react'
import { useStore } from '../store.js'
import { typeColor } from '../constants.js'

interface Hit {
  id: string
  type: string
  label: string
  score: number
  matched: string
}

export function SearchPanel(): JSX.Element {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<Hit[]>([])
  const [busy, setBusy] = useState(false)
  const runPath = useStore((s) => s.runPath)
  const select = useStore((s) => s.select)
  const setHighlight = useStore((s) => s.setHighlight)
  const focusNodes = useStore((s) => s.focusNodes)

  const search = async (text: string) => {
    setQ(text)
    const rpc = useStore.getState().rpc
    if (!rpc || !runPath || !text.trim()) {
      setHits([])
      return
    }
    setBusy(true)
    try {
      const res = await rpc.call<Hit[]>('search', { runPath, query: text, limit: 40 })
      setHits(res)
      setHighlight(res.map((h) => h.id))
    } finally {
      setBusy(false)
    }
  }

  const pick = (h: Hit) => {
    select(h.id)
    setHighlight([h.id])
    focusNodes([h.id], true)
  }

  return (
    <div className="panel-section">
      <div className="section-title">Search</div>
      <input
        className="search-input"
        placeholder="name, title, alias, property…"
        value={q}
        onChange={(e) => void search(e.target.value)}
      />
      <div className="hit-list">
        {busy && <div className="muted small">searching…</div>}
        {!busy && q && hits.length === 0 && <div className="muted small">no matches</div>}
        {hits.map((h) => (
          <div key={h.id} className="hit-row" onClick={() => pick(h)} title={h.id}>
            <span className="type-dot" style={{ background: typeColor(h.type) }} />
            <span className="hit-label">{h.label}</span>
            <span className="hit-type">{h.type}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
