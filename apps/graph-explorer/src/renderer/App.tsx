import { useEffect, useState } from 'react'
import { WorldLoading, WorldLoadingOverlay } from './components/WorldLoading.js'
import { useStore, type View } from './store.js'
import { GraphView } from './components/GraphView.js'
import { SearchPanel } from './components/SearchPanel.js'
import { Legend } from './components/Legend.js'
import { TimeScrubber } from './components/TimeScrubber.js'
import { DetailsPanel } from './components/DetailsPanel.js'
import { ChatPanel } from './components/ChatPanel.js'
import { QueryConsole } from './components/QueryConsole.js'
import { DiffPanel } from './components/DiffPanel.js'
import { WorldsView } from './views/WorldsView.js'
import { TemplatesView } from './views/TemplatesView.js'
import { EvalsView } from './views/EvalsView.js'

type Tab = 'chat' | 'details' | 'query' | 'diff'

const VIEWS: { id: View; label: string }[] = [
  { id: 'explore', label: 'Explore' },
  { id: 'worlds', label: 'Worlds' },
  { id: 'templates', label: 'Templates' },
  { id: 'evals', label: 'Evals' }
]

export function App(): JSX.Element {
  const init = useStore((s) => s.init)
  const connecting = useStore((s) => s.connecting)
  const connectError = useStore((s) => s.connectError)
  const worlds = useStore((s) => s.worlds)
  const worldPath = useStore((s) => s.worldPath)
  const model = useStore((s) => s.model)
  const schema = useStore((s) => s.schema)
  const loadingWorld = useStore((s) => s.loadingWorld)
  const loadingWorldPath = useStore((s) => s.loadingWorldPath)
  const connection = useStore((s) => s.connection)
  const loadWorld = useStore((s) => s.loadWorld)
  const pickWorldDir = useStore((s) => s.pickWorldDir)
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
          value={loadingWorldPath ?? worldPath ?? ''}
          disabled={loadingWorld}
          onChange={(e) => void loadWorld(e.target.value)}
        >
          {worlds.length === 0 && <option value="">no worlds found</option>}
          {worlds.map((w) => (
            <option key={w.world_dir} value={w.world_dir} disabled={!w.has_kg}>
              {w.world_id}
              {w.has_kg ? ` · ${w.counts.nodes ?? '?'}n/${w.counts.edges ?? '?'}e` : ' · no graph yet'}
              {w.status !== 'done' ? ` · ${w.status}` : ''}
            </option>
          ))}
        </select>
        <button className="btn ghost" disabled={loadingWorld} onClick={() => void pickWorldDir()}>
          Open…
        </button>
        <WorldLoading />
        {connection === 'reconnecting' && (
          <span className="world-loading-chip reconnecting" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            Reconnecting to the graph engine…
          </span>
        )}
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

      {view === 'worlds' && <WorldsView />}
      {view === 'templates' && <TemplatesView />}
      {view === 'evals' && <EvalsView />}
      <div className="body" style={view === 'explore' ? undefined : { display: 'none' }}>
        <aside className="sidebar">
          <SearchPanel />
          <Legend />
        </aside>

        <main className="center">
          {model && <GraphView />}
          <WorldLoadingOverlay />
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
