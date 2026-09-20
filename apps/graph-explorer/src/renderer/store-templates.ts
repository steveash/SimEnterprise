// Store slice for the Templates feature (docs/EXPLORER_TEMPLATES.md §4). Kept as
// its own zustand store per the app's file-ownership split — `store.ts` is not
// touched. Cross-feature reads go through `useStore.getState()` (currently just
// the shared `Rpc` connection the sidecar init already set up).
import { create } from 'zustand'
import { useStore } from './store.js'
import type {
  Template,
  TemplateAgentEvent,
  TemplateKind,
  TemplateListEntry,
  TemplatesReadResult,
  ValidationReport
} from '../shared/templates-protocol.js'

export interface TemplateTraceEntry {
  id: string
  name: string
  input: unknown
  ok?: boolean
  preview?: string
}

export interface TemplateChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  thinking?: string
  trace: TemplateTraceEntry[]
  done: boolean
  error?: string
}

interface ChatState {
  messages: TemplateChatMessage[]
  active: { id: string; cancel: () => void } | null
  conversationId: string
}

function newChatState(): ChatState {
  return { messages: [], active: null, conversationId: `tc${Date.now()}${Math.random().toString(36).slice(2, 6)}` }
}

function rpc() {
  const r = useStore.getState().rpc
  if (!r) throw new Error('sidecar not connected')
  return r
}

interface TemplatesState {
  entries: TemplateListEntry[]
  loadingList: boolean
  listError: string | null

  selectedSlug: string | null
  validationBySlug: Record<string, ValidationReport | null>

  fileViewer: TemplatesReadResult | null
  loadingFiles: boolean

  chatBySlug: Record<string, ChatState>
  modelId: string

  scaffolding: boolean
  scaffoldError: string | null
  deleteError: string | null

  loadList: () => Promise<void>
  selectTemplate: (slug: string | null) => void
  scaffold: (input: { slug: string; kind: TemplateKind; name: string; description: string }) => Promise<Template | null>
  validate: (slug: string) => Promise<void>
  deleteTemplate: (slug: string) => Promise<void>
  loadFiles: (slug: string) => Promise<void>
  setModelId: (m: string) => void
  sendChat: (slug: string, text: string) => void
  cancelChat: (slug: string) => void
}

function ensureChat(state: TemplatesState, slug: string): ChatState {
  return state.chatBySlug[slug] ?? newChatState()
}

export const useTemplatesStore = create<TemplatesState>((set, get) => ({
  entries: [],
  loadingList: false,
  listError: null,

  selectedSlug: null,
  validationBySlug: {},

  fileViewer: null,
  loadingFiles: false,

  chatBySlug: {},
  modelId: 'sonnet',

  scaffolding: false,
  scaffoldError: null,
  deleteError: null,

  loadList: async () => {
    set({ loadingList: true, listError: null })
    try {
      const entries = await rpc().call<TemplateListEntry[]>('templatesList')
      const validationBySlug = { ...get().validationBySlug }
      for (const e of entries) {
        if (e.loadable && !(e.slug in validationBySlug)) {
          validationBySlug[e.slug] = {
            slug: e.slug,
            ok: e.validation.status === 'valid',
            steps: [],
            provides: e.provides
          }
        }
      }
      set({ entries, loadingList: false, validationBySlug })
    } catch (e) {
      set({ loadingList: false, listError: (e as Error).message })
    }
  },

  selectTemplate: (slug) => set({ selectedSlug: slug, fileViewer: null }),

  scaffold: async (input) => {
    set({ scaffolding: true, scaffoldError: null })
    try {
      const template = await rpc().call<Template>('templatesScaffold', input)
      set({ scaffolding: false, selectedSlug: template.slug })
      await get().loadList()
      return template
    } catch (e) {
      set({ scaffolding: false, scaffoldError: (e as Error).message })
      return null
    }
  },

  validate: async (slug) => {
    try {
      const report = await rpc().call<ValidationReport>('templatesValidate', { slug })
      set((st) => ({ validationBySlug: { ...st.validationBySlug, [slug]: report } }))
      await get().loadList()
    } catch (e) {
      set((st) => ({
        validationBySlug: {
          ...st.validationBySlug,
          [slug]: { slug, ok: false, steps: [{ step: 'import', ok: false, detail: (e as Error).message }], provides: { archetypes: [], playbooks: [], processes: [] } }
        }
      }))
    }
  },

  deleteTemplate: async (slug) => {
    set({ deleteError: null })
    try {
      await rpc().call('templatesDelete', { slug })
      set((st) => ({
        entries: st.entries.filter((e) => e.slug !== slug),
        selectedSlug: st.selectedSlug === slug ? null : st.selectedSlug,
        fileViewer: st.fileViewer?.slug === slug ? null : st.fileViewer
      }))
    } catch (e) {
      set({ deleteError: (e as Error).message })
    }
  },

  loadFiles: async (slug) => {
    set({ loadingFiles: true })
    try {
      const result = await rpc().call<TemplatesReadResult>('templatesRead', { slug })
      set({ fileViewer: result, loadingFiles: false })
    } catch (e) {
      set({ loadingFiles: false, fileViewer: { slug, files: [{ path: 'error', content: (e as Error).message }] } })
    }
  },

  setModelId: (m) => set({ modelId: m }),

  sendChat: (slug, text) => {
    if (!text.trim()) return
    const state = get()
    if (state.chatBySlug[slug]?.active) return
    const chat = ensureChat(state, slug)
    const userMsg: TemplateChatMessage = { id: `u${Date.now()}`, role: 'user', text, trace: [], done: true }
    const asstId = `a${Date.now()}`
    const asstMsg: TemplateChatMessage = { id: asstId, role: 'assistant', text: '', trace: [], done: false }
    const messages = [...chat.messages, userMsg, asstMsg]
    set((st) => ({ chatBySlug: { ...st.chatBySlug, [slug]: { ...chat, messages } } }))

    const update = (fn: (m: TemplateChatMessage) => TemplateChatMessage) =>
      set((st) => {
        const c = st.chatBySlug[slug]
        if (!c) return st
        return { chatBySlug: { ...st.chatBySlug, [slug]: { ...c, messages: c.messages.map((m) => (m.id === asstId ? fn(m) : m)) } } }
      })

    const handle = (e: TemplateAgentEvent) => {
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
          update((m) => ({ ...m, trace: m.trace.map((t) => (t.id === e.id ? { ...t, ok: e.ok, preview: e.preview } : t)) }))
          break
        case 'template':
          set((st) => ({ validationBySlug: { ...st.validationBySlug, [e.slug]: e.validation } }))
          break
        case 'final_query':
          break // not applicable to template authoring
        case 'viz':
          break // not applicable to template authoring
        case 'error':
          update((m) => ({ ...m, error: e.message }))
          break
        case 'done':
          update((m) => ({ ...m, done: true }))
          set((st) => ({ chatBySlug: { ...st.chatBySlug, [slug]: { ...st.chatBySlug[slug], active: null } } }))
          break
      }
    }

    const active = rpc().stream<TemplateAgentEvent>(
      'templatesChat',
      { slug, prompt: text, model: get().modelId, conversationId: chat.conversationId },
      handle,
      'templatesCancelChat'
    )
    set((st) => ({ chatBySlug: { ...st.chatBySlug, [slug]: { ...st.chatBySlug[slug], active } } }))
  },

  cancelChat: (slug) => {
    const chat = get().chatBySlug[slug]
    chat?.active?.cancel()
    set((st) => ({ chatBySlug: { ...st.chatBySlug, [slug]: { ...st.chatBySlug[slug], active: null } } }))
  }
}))

/** Kick off the authoring chat for a freshly scaffolded template with the standard first message. */
export function openAuthoringChat(slug: string, kind: TemplateKind, description: string): void {
  useTemplatesStore.getState().selectTemplate(slug)
  useTemplatesStore.getState().sendChat(slug, `Author the \`${slug}\` template (${kind}): ${description}`)
}
