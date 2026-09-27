import { create } from 'zustand'
import { Rpc } from './rpc.js'
import type { GraphModel, WorldSummary } from '../shared/model.js'
import type { LoadWorldResult, DiffResult } from '../shared/protocol.js'
import type { AgentEvent, VizEvent, FinalQuery } from '../shared/agent-events.js'
import type { Lens, LensInput } from '../shared/lenses.js'

export interface ChatTurn {
  role: 'user' | 'assistant'
  text: string
}

export interface TraceEntry {
  id: string
  name: string
  input: unknown
  ok?: boolean
  preview?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  thinking?: string
  trace: TraceEntry[]
  /** The engine + exact query that produced this turn's answer, if any. */
  finalQuery?: FinalQuery
  done: boolean
  error?: string
}

interface FocusRequest {
  nodeIds: string[]
  fit: boolean
  nonce: number
}

/** Top-level views (docs/EXPLORER.md §2). `explore` is the original graph explorer. */
export type View = 'explore' | 'worlds' | 'templates' | 'evals'

interface AppState {
  /** Top-level view (docs/EXPLORER.md §2). */
  view: View
  setView: (v: View) => void

  rpc: Rpc | null
  /** Live state of the sidecar websocket (drives the topbar reconnect chip). */
  connection: 'connecting' | 'open' | 'reconnecting'
  connecting: boolean
  connectError: string | null

  /** Every world under the worlds root (the same list the Worlds tab shows). */
  worlds: WorldSummary[]
  refreshWorlds: () => Promise<void>
  worldPath: string | null
  model: GraphModel | null
  schema: LoadWorldResult | null
  loadingWorld: boolean
  /** The world being loaded right now (drives the topbar progress + overlay), with its start time. */
  loadingWorldPath: string | null
  loadingStartedAt: number | null

  // visibility / filters
  visibleTypes: Set<string>
  visibleEdgeTypes: Set<string>
  timeCursor: number | null // ms epoch; nodes/events created after are dimmed
  layout: 'fcose' | 'dagre' | 'concentric'

  // selection / highlight
  selectedId: string | null
  highlightNodes: Set<string>
  highlightEdges: Set<string>
  focus: FocusRequest | null

  // chat
  chat: ChatMessage[]
  chatActive: { id: string; cancel: () => void } | null
  conversationId: string
  model_id: string

  // diff
  diff: DiffResult | null

  // saved query lenses (persisted in the main process)
  lenses: Lens[]

  // actions
  init: () => Promise<void>
  loadWorld: (path: string) => Promise<void>
  pickWorldDir: () => Promise<void>
  select: (id: string | null) => void
  setHighlight: (nodes: string[], edges?: string[]) => void
  clearHighlight: () => void
  focusNodes: (ids: string[], fit?: boolean) => void
  toggleType: (t: string) => void
  toggleEdgeType: (t: string) => void
  setLayout: (l: AppState['layout']) => void
  setTimeCursor: (ms: number | null) => void
  applyViz: (v: VizEvent) => void
  sendChat: (text: string) => void
  cancelChat: () => void
  setModelId: (m: string) => void
  runDiff: (otherPath: string) => Promise<void>
  loadLenses: () => Promise<void>
  saveLens: (input: LensInput) => Promise<void>
  deleteLens: (id: string) => Promise<void>
}

export const useStore = create<AppState>((set, get) => ({
  rpc: null,
  connection: 'connecting',
  connecting: true,
  connectError: null,
  worlds: [],
  worldPath: null,
  model: null,
  schema: null,
  loadingWorld: false,
  loadingWorldPath: null,
  loadingStartedAt: null,
  visibleTypes: new Set(),
  visibleEdgeTypes: new Set(),
  timeCursor: null,
  layout: 'fcose',
  selectedId: null,
  highlightNodes: new Set(),
  highlightEdges: new Set(),
  focus: null,
  chat: [],
  chatActive: null,
  conversationId: `c${Date.now()}`,
  model_id: 'sonnet',
  diff: null,
  lenses: [],

  view: 'explore',
  setView: (v) => set({ view: v }),
  init: async () => {
    try {
      const info = await window.explorer.sidecarInfo()
      if (info.error || !info.port) {
        set({ connecting: false, connectError: info.error ?? 'sidecar unavailable' })
        return
      }
      const rpc = new Rpc(info.port)
      let wasOpen = false
      rpc.onStatus((connection) => {
        set({ connection })
        // Back after a drop (e.g. the sidecar restarted): the world list may have
        // changed, and the sidecar reloads a world lazily on its next query.
        if (connection === 'open' && wasOpen) void get().refreshWorlds()
        if (connection === 'open') wasOpen = true
      })
      const worlds = await rpc.call<WorldSummary[]>('listWorlds')
      set({ rpc, connecting: false, worlds })
      await get().loadLenses()
      const first = worlds.find((w) => w.has_kg)
      if (first) await get().loadWorld(first.world_dir)
    } catch (e) {
      set({ connecting: false, connectError: (e as Error).message })
    }
  },

  refreshWorlds: async () => {
    const rpc = get().rpc
    if (!rpc) return
    try {
      set({ worlds: await rpc.call<WorldSummary[]>('listWorlds') })
    } catch {
      /* keep the last list */
    }
  },

  loadWorld: async (path) => {
    const rpc = get().rpc
    if (!rpc) return
    set({ loadingWorld: true, loadingWorldPath: path, loadingStartedAt: Date.now(), diff: null })
    try {
      const res = await rpc.call<LoadWorldResult>('loadWorld', { path })
      set({
        worldPath: path,
        model: res.model,
        schema: res,
        loadingWorld: false,
        loadingWorldPath: null,
        loadingStartedAt: null,
        // CalendarEvent + has_calendar_event are noisy; start hidden but toggleable.
        visibleTypes: new Set(res.model.nodeTypes.filter((t) => t !== 'CalendarEvent')),
        visibleEdgeTypes: new Set(res.model.edgeTypes.filter((t) => t !== 'has_calendar_event')),
        selectedId: null,
        highlightNodes: new Set(),
        highlightEdges: new Set(),
        timeCursor: null,
        focus: { nodeIds: [], fit: true, nonce: Date.now() }
      })
    } catch (e) {
      set({ loadingWorld: false, loadingWorldPath: null, loadingStartedAt: null, connectError: (e as Error).message })
    }
  },

  pickWorldDir: async () => {
    const dir = await window.explorer.pickWorldDir()
    if (dir) await get().loadWorld(dir)
  },

  select: (id) => {
    set({ selectedId: id })
  },

  setHighlight: (nodes, edges) =>
    set({ highlightNodes: new Set(nodes), highlightEdges: new Set(edges ?? []) }),

  clearHighlight: () => set({ highlightNodes: new Set(), highlightEdges: new Set() }),

  focusNodes: (ids, fit = true) => set({ focus: { nodeIds: ids, fit, nonce: Date.now() } }),

  toggleType: (t) => {
    const s = new Set(get().visibleTypes)
    s.has(t) ? s.delete(t) : s.add(t)
    set({ visibleTypes: s })
  },

  toggleEdgeType: (t) => {
    const s = new Set(get().visibleEdgeTypes)
    s.has(t) ? s.delete(t) : s.add(t)
    set({ visibleEdgeTypes: s })
  },

  setLayout: (l) => set({ layout: l }),
  setTimeCursor: (ms) => set({ timeCursor: ms }),
  setModelId: (m) => set({ model_id: m }),

  applyViz: (v) => {
    set({ highlightNodes: new Set(v.nodeIds), highlightEdges: new Set(v.edgeIds ?? []) })
    if (v.type === 'focus' && v.nodeIds.length) get().focusNodes(v.nodeIds, true)
  },

  sendChat: (text) => {
    const { rpc, worldPath, chat, model_id, conversationId } = get()
    if (!rpc || !worldPath || !text.trim()) return
    const userMsg: ChatMessage = { id: `u${Date.now()}`, role: 'user', text, trace: [], done: true }
    const asstId = `a${Date.now()}`
    const asstMsg: ChatMessage = { id: asstId, role: 'assistant', text: '', trace: [], done: false }
    set({ chat: [...chat, userMsg, asstMsg] })

    const update = (fn: (m: ChatMessage) => ChatMessage) =>
      set((st) => ({ chat: st.chat.map((m) => (m.id === asstId ? fn(m) : m)) }))

    const handle = (e: AgentEvent) => {
      switch (e.kind) {
        case 'text':
          update((m) => ({ ...m, text: m.text + e.text }))
          break
        case 'thinking':
          update((m) => ({ ...m, thinking: (m.thinking ?? '') + e.text }))
          break
        case 'tool_use':
          update((m) => ({ ...m, trace: [...m.trace, { id: e.id, name: e.name, input: e.input }] }))
          break
        case 'tool_result':
          update((m) => ({
            ...m,
            trace: m.trace.map((t) => (t.id === e.id ? { ...t, ok: e.ok, preview: e.preview } : t))
          }))
          break
        case 'viz':
          get().applyViz(e.viz)
          break
        case 'final_query':
          update((m) => ({ ...m, finalQuery: { engine: e.engine, query: e.query } }))
          break
        case 'error':
          update((m) => ({ ...m, error: e.message }))
          break
        case 'done':
          update((m) => ({ ...m, done: true }))
          set({ chatActive: null })
          break
      }
    }

    const active = rpc.chat({ worldPath, prompt: text, model: model_id, conversationId }, handle)
    set({ chatActive: active })
  },

  cancelChat: () => {
    get().chatActive?.cancel()
    set({ chatActive: null })
  },

  runDiff: async (otherPath) => {
    const { rpc, worldPath } = get()
    if (!rpc || !worldPath) return
    const diff = await rpc.call<DiffResult>('diffWorlds', { pathA: worldPath, pathB: otherPath })
    set({ diff })
  },

  loadLenses: async () => {
    set({ lenses: await window.explorer.lenses.list() })
  },

  saveLens: async (input) => {
    set({ lenses: await window.explorer.lenses.save(input) })
  },

  deleteLens: async (id) => {
    set({ lenses: await window.explorer.lenses.delete(id) })
  }
}))
