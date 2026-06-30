import { create } from 'zustand'
import { Rpc } from './rpc.js'
import type { GraphModel, RunSummary } from '../shared/model.js'
import type { LoadRunResult, DiffResult } from '../shared/protocol.js'
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

interface AppState {
  rpc: Rpc | null
  connecting: boolean
  connectError: string | null
  hasApiKey: boolean

  runs: RunSummary[]
  runPath: string | null
  model: GraphModel | null
  schema: LoadRunResult | null
  loadingRun: boolean

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
  loadRun: (path: string) => Promise<void>
  pickRunDir: () => Promise<void>
  setApiKey: (key: string) => Promise<void>
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
  connecting: true,
  connectError: null,
  hasApiKey: false,
  runs: [],
  runPath: null,
  model: null,
  schema: null,
  loadingRun: false,
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

  init: async () => {
    try {
      const info = await window.explorer.sidecarInfo()
      if (info.error || !info.port) {
        set({ connecting: false, connectError: info.error ?? 'sidecar unavailable' })
        return
      }
      const rpc = new Rpc(info.port)
      const runs = await rpc.call<RunSummary[]>('listRuns')
      set({ rpc, connecting: false, runs, hasApiKey: info.hasApiKey })
      await get().loadLenses()
      if (runs.length) await get().loadRun(runs[0].runPath)
    } catch (e) {
      set({ connecting: false, connectError: (e as Error).message })
    }
  },

  loadRun: async (path) => {
    const rpc = get().rpc
    if (!rpc) return
    set({ loadingRun: true, diff: null })
    try {
      const res = await rpc.call<LoadRunResult>('loadRun', { path })
      set({
        runPath: path,
        model: res.model,
        schema: res,
        loadingRun: false,
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
      set({ loadingRun: false, connectError: (e as Error).message })
    }
  },

  pickRunDir: async () => {
    const dir = await window.explorer.pickRunDir()
    if (dir) await get().loadRun(dir)
  },

  setApiKey: async (key) => {
    const res = await window.explorer.setApiKey(key)
    if (res.ok && res.port) {
      const rpc = new Rpc(res.port)
      set({ rpc, hasApiKey: true })
      const path = get().runPath
      if (path) {
        // re-establish loaded state on the new sidecar
        await rpc.call('loadRun', { path })
      }
    }
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
    const { rpc, runPath, chat, model_id, conversationId } = get()
    if (!rpc || !runPath || !text.trim()) return
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

    const active = rpc.chat({ runPath, prompt: text, model: model_id, conversationId }, handle)
    set({ chatActive: active })
  },

  cancelChat: () => {
    get().chatActive?.cancel()
    set({ chatActive: null })
  },

  runDiff: async (otherPath) => {
    const { rpc, runPath } = get()
    if (!rpc || !runPath) return
    const diff = await rpc.call<DiffResult>('diffRuns', { pathA: runPath, pathB: otherPath })
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
