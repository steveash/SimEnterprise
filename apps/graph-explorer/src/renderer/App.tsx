import { useEffect, useState } from 'react'
import { useStore, type View } from './store.js'
import { GraphView } from './components/GraphView.js'
import { SearchPanel } from './components/SearchPanel.js'
import { Legend } from './components/Legend.js'
import { TimeScrubber } from './components/TimeScrubber.js'
import { DetailsPanel } from './components/DetailsPanel.js'
import { ChatPanel } from './components/ChatPanel.js'
import { QueryConsole } from './components/QueryConsole.js'
import { DiffPanel } from './components/DiffPanel.js'
import { RunsView } from './views/RunsView.js'
import { TemplatesView } from './views/TemplatesView.js'
import { EvalsView } from './views/EvalsView.js'

type Tab = 'chat' | 'details' | 'query' | 'diff'

const VIEWS: { id: View; label: string }[] = [
  { id: 'explore', label: 'Explore' },
  { id: 'runs', label: 'Runs' },
  { id: 'templates', label: 'Templates' },
  { id: 'evals', label: 'Evals' }
]

export function App(): JSX.Element {
  const init = useStore((s) => s.init)
  const connecting = useStore((s) => s.connecting)
  const connectError = useStore((s) => s.connectError)
  const runs = useStore((s) => s.runs)
  const runPath = useStore((s) => s.runPath)
  const model = useStore((s) => s.model)
  const schema = useStore((s) => s.schema)
  const loadingRun = useStore((s) => s.loadingRun)
  const loadRun = useStore((s) => s.loadRun)
  const pickRunDir = useStore((s) => s.pickRunDir)
  const layout = useStore((s) => s.layout)
  const setLayout = useStore((s) => s.setLayout)
  const clearHighlight = useStore((s) => s.clearHighlight)
  const focusNodes = useStore((s) => s.focusNodes)
  const selectedId = useStore((s) => s.selectedId)
  const view = useStore((s) => s.view)
  const setView = useStore((s) => s.setView)

  const [tab, setTab] = useState<Tab>('chat')

  useEffect(() => {
    void init()
  }, [init])

  // jump to details tab when a node is selected
  useEffect(() => {
    if (selectedId) setTab('details')
  }, [selectedId])

  if (connecting) return <div className="boot">Connecting to graph engine…</div>
  if (connectError)
    return (
      <div className="boot error">
        <div>Could not start the graph engine sidecar.</div>
        <pre className="mono small">{connectError}</pre>
      </div>
    )

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Enterprise-Sim · Graph Explorer</div>
        <nav className="view-switch" aria-label="views">
          {VIEWS.map((v) => (
            <button key={v.id} className={view === v.id ? 'active' : ''} onClick={() => setView(v.id)}>
              {v.label}
            </button>
          ))}
        </nav>
        <select
          className="run-select"
          value={runPath ?? ''}
          onChange={(e) => void loadRun(e.target.value)}
        >
          {runs.length === 0 && <option value="">no runs found</option>}
          {runs.map((r) => (
            <option key={r.runPath} value={r.runPath}>
              {r.runId} · {r.nodeCount}n/{r.edgeCount}e
            </option>
          ))}
        </select>
        <button className="btn ghost" onClick={() => void pickRunDir()}>
          Open…
        </button>
        <div className="spacer" />
        {schema?.eval && (
          <div className="eval-badges">
            {schema.eval.metrics.map((m) => (
              <span key={m.name} className={`eval-badge ${m.ok ? 'ok' : 'bad'}`} title={m.detail}>
                {m.name}: {Math.round(m.score * 100)}%
              </span>
            ))}
            <span className="eval-badge muted" title="inferred RDF triples added by the ontology">
              +{schema.inferredCount} inferred
            </span>
          </div>
        )}
        {view === 'explore' && (
          <>
            <div className="layout-controls">
              {(['fcose', 'dagre', 'concentric'] as const).map((l) => (
                <button key={l} className={layout === l ? 'active' : ''} onClick={() => setLayout(l)}>
                  {l === 'fcose' ? 'force' : l === 'dagre' ? 'hierarchy' : 'radial'}
                </button>
              ))}
            </div>
            <button className="btn ghost" onClick={() => { clearHighlight(); focusNodes([], true) }}>
              Reset view
            </button>
          </>
        )}
      </header>

      {view === 'runs' && <RunsView />}
      {view === 'templates' && <TemplatesView />}
      {view === 'evals' && <EvalsView />}
      <div className="body" style={view === 'explore' ? undefined : { display: 'none' }}>
        <aside className="sidebar">
          <SearchPanel />
          <Legend />
        </aside>

        <main className="center">
          {loadingRun && <div className="loading-overlay">loading run…</div>}
          {model && <GraphView />}
          <TimeScrubber />
        </main>

        <aside className="rightbar">
          <div className="tabs">
            {(['chat', 'details', 'query', 'diff'] as const).map((t) => (
              <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
                {t === 'chat' ? 'Agent' : t === 'query' ? 'Query' : t.charAt(0).toUpperCase() + t.slice(1)}
              </button>
            ))}
          </div>
          <div className="tab-body">
            {tab === 'chat' && <ChatPanel />}
            {tab === 'details' && <DetailsPanel />}
            {tab === 'query' && <QueryConsole />}
            {tab === 'diff' && <DiffPanel />}
          </div>
        </aside>
      </div>
    </div>
  )
}
