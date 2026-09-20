// The Evals view's own zustand store (docs/EXPLORER_EVALS.md §5). Kept separate
// from `store.ts` per file ownership; cross-feature actions (loading a run,
// highlighting/focusing nodes on the Explore graph) go through
// `useStore.getState()` rather than duplicating that state here.
import { create } from 'zustand'
import { useStore } from './store.js'
import type {
  Aggregate,
  EvalExecutionMeta,
  EvalRunner,
  EvalSelection,
  EvalStreamEvent,
  ItemScore,
  ProposedQuestion,
  ProposeStreamEvent,
  QAPair,
  QuestionCounts,
  TraceEntry
} from '../shared/evals-protocol.js'

export type GroupBy = 'reasoning_type' | 'qtype' | 'difficulty' | 'source' | 'tag'

export interface ProposeMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  thinking?: string
  done: boolean
  error?: string
}

export interface ReviewItem {
  key: string
  proposal: ProposedQuestion
  valid: boolean
  reason?: string
  checked: boolean
}

interface RunProgress {
  done: number
  total: number
  macroF1SoFar: number
  byReasoningType: Record<string, Aggregate>
}

interface EvalsState {
  // question browser
  questions: QAPair[]
  counts: QuestionCounts | null
  expectedLabels: Record<string, string>
  loadingQuestions: boolean
  hasQuestionSet: boolean
  loadError: string | null
  search: string
  groupBy: GroupBy
  selected: Set<string>
  detailId: string | null

  // run controls
  runner: EvalRunner
  model: string
  concurrency: number
  sampleFraction: number
  sampleSeed: number
  sampleStratify: boolean
  activeRun: { id: string; cancel: () => void } | null
  runProgress: RunProgress | null
  runError: string | null

  // executions
  results: EvalExecutionMeta[]
  loadingResults: boolean
  selectedResultId: string | null

  /** Per-question live trace + final engine/query from the most recent single-question run (§3/§5). */
  liveTraces: Record<string, { engine?: string; query?: string; trace: TraceEntry[] }>

  // propose tab
  proposeChat: ProposeMessage[]
  proposeActive: { id: string; cancel: () => void } | null
  proposeConversationId: string
  proposeModel: string
  reviewList: ReviewItem[]
  addingToSet: boolean

  // actions
  loadQuestions: (runPath: string) => Promise<void>
  generateQuestions: (runPath: string) => Promise<void>
  setSearch: (s: string) => void
  setGroupBy: (g: GroupBy) => void
  toggleSelected: (id: string) => void
  selectAll: (ids: string[]) => void
  clearSelected: () => void
  setDetailId: (id: string | null) => void
  openInExplore: (ids: string[]) => void

  setRunner: (r: EvalRunner) => void
  setModel: (m: string) => void
  setConcurrency: (n: number) => void
  setSampleFraction: (f: number) => void
  setSampleSeed: (s: number) => void
  setSampleStratify: (b: boolean) => void
  runQuestions: (runPath: string, selection: EvalSelection) => void
  cancelRun: () => void

  loadResults: (runPath: string) => Promise<void>
  selectResult: (id: string | null) => void
  latestScoreFor: (qaId: string) => ItemScore | null

  setProposeModel: (m: string) => void
  sendPropose: (runPath: string, text: string) => void
  cancelPropose: () => void
  toggleReviewChecked: (key: string) => void
  clearReview: () => void
  addCheckedToSet: (runPath: string) => Promise<void>
  removeQuestions: (runPath: string, ids: string[]) => Promise<void>
}

function emptyCounts(): QuestionCounts {
  return { reasoning_type: {}, qtype: {}, difficulty: {}, source: {} }
}

export const useEvalsStore = create<EvalsState>((set, get) => ({
  questions: [],
  counts: null,
  expectedLabels: {},
  // Starts true so the view shows "loading" rather than flashing "no question
  // set" before the first `loadQuestions` (triggered by EvalsView's mount effect)
  // resolves.
  loadingQuestions: true,
  hasQuestionSet: false,
  loadError: null,
  search: '',
  groupBy: 'reasoning_type',
  selected: new Set(),
  detailId: null,

  runner: 'explorer',
  model: 'sonnet',
  concurrency: 2,
  sampleFraction: 0.2,
  sampleSeed: 0,
  sampleStratify: true,
  activeRun: null,
  runProgress: null,
  runError: null,

  results: [],
  loadingResults: false,
  selectedResultId: null,
  liveTraces: {},

  proposeChat: [],
  proposeActive: null,
  proposeConversationId: `evc${Date.now()}`,
  proposeModel: 'sonnet',
  reviewList: [],
  addingToSet: false,

  loadQuestions: async (runPath) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ loadingQuestions: true, loadError: null })
    try {
      const res = await rpc.call<{
        questions: QAPair[]
        counts: QuestionCounts
        expected_labels: Record<string, string>
      }>('evalsList', { runPath })
      set({
        questions: res.questions,
        counts: res.counts ?? emptyCounts(),
        expectedLabels: res.expected_labels ?? {},
        hasQuestionSet: res.questions.length > 0,
        loadingQuestions: false
      })
    } catch (e) {
      set({ loadingQuestions: false, loadError: (e as Error).message })
    }
  },

  generateQuestions: async (runPath) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ loadingQuestions: true, loadError: null })
    try {
      await rpc.call('evalsGenerate', { runPath })
      await get().loadQuestions(runPath)
    } catch (e) {
      set({ loadingQuestions: false, loadError: (e as Error).message })
    }
  },

  setSearch: (s) => set({ search: s }),
  setGroupBy: (g) => set({ groupBy: g }),
  toggleSelected: (id) => {
    const s = new Set(get().selected)
    if (s.has(id)) s.delete(id)
    else s.add(id)
    set({ selected: s })
  },
  selectAll: (ids) => set({ selected: new Set(ids) }),
  clearSelected: () => set({ selected: new Set() }),
  setDetailId: (id) => set({ detailId: id }),

  openInExplore: (ids) => {
    const { setHighlight, focusNodes, setView } = useStore.getState()
    setHighlight(ids)
    focusNodes(ids, true)
    setView('explore')
  },

  setRunner: (r) => set({ runner: r }),
  setModel: (m) => set({ model: m }),
  setConcurrency: (n) => set({ concurrency: Math.max(1, Math.min(4, Math.round(n))) }),
  setSampleFraction: (f) => set({ sampleFraction: f }),
  setSampleSeed: (s) => set({ sampleSeed: Math.round(s) }),
  setSampleStratify: (b) => set({ sampleStratify: b }),

  runQuestions: (runPath, selection) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    const { runner, model, concurrency } = get()
    const isSingle = selection.kind === 'ids' && selection.ids.length === 1
    set({ runProgress: null, runError: null })
    const handle = rpc.stream<EvalStreamEvent, EvalExecutionMeta>(
      'evalsRun',
      { runPath, selection, runner, model, concurrency },
      (e) => {
        switch (e.kind) {
          case 'progress':
            set({
              runProgress: { done: e.done, total: e.total, macroF1SoFar: e.macro_f1_so_far, byReasoningType: e.by_reasoning_type }
            })
            break
          case 'question_done':
            if (e.trace) {
              set((st) => ({ liveTraces: { ...st.liveTraces, [e.id]: { engine: e.engine, query: e.query, trace: e.trace! } } }))
            }
            if (isSingle) {
              // Highlight predicted + expected together (single-color: the tri-color
              // correct/wrong/missed distinction lives in the question detail's chips,
              // which already color hits/misses; the shared Cytoscape canvas only
              // exposes one highlight set).
              const { setHighlight, focusNodes } = useStore.getState()
              const union = [...new Set([...e.predicted_ids, ...e.expected_ids])]
              setHighlight(union)
              focusNodes(union, true)
            }
            break
          case 'error':
            set({ runError: e.message })
            break
          case 'done':
            void get().loadResults(runPath)
            break
          default:
            break
        }
      },
      'evalsCancel'
    )
    set({ activeRun: { id: handle.id, cancel: handle.cancel } })
    handle.done
      .catch((e: Error) => set({ runError: e.message }))
      .finally(() => {
        set({ activeRun: null })
        void get().loadResults(runPath)
      })
  },

  cancelRun: () => {
    get().activeRun?.cancel()
  },

  loadResults: async (runPath) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ loadingResults: true })
    try {
      const results = await rpc.call<EvalExecutionMeta[]>('evalsListResults', { runPath })
      set({ results, loadingResults: false })
    } catch {
      set({ loadingResults: false })
    }
  },

  selectResult: (id) => set({ selectedResultId: id }),

  latestScoreFor: (qaId) => {
    for (const r of get().results) {
      if (!r.report) continue
      const item = r.report.items.find((it) => it.qa_id === qaId)
      if (item) return item
    }
    return null
  },

  setProposeModel: (m) => set({ proposeModel: m }),

  sendPropose: (runPath, text) => {
    const rpc = useStore.getState().rpc
    if (!rpc || !text.trim()) return
    const { proposeChat, proposeModel, proposeConversationId } = get()
    const userMsg: ProposeMessage = { id: `u${Date.now()}`, role: 'user', text, done: true }
    const asstId = `a${Date.now()}`
    const asstMsg: ProposeMessage = { id: asstId, role: 'assistant', text: '', done: false }
    set({ proposeChat: [...proposeChat, userMsg, asstMsg] })

    const update = (fn: (m: ProposeMessage) => ProposeMessage) =>
      set((st) => ({ proposeChat: st.proposeChat.map((m) => (m.id === asstId ? fn(m) : m)) }))

    const handle = rpc.stream<ProposeStreamEvent, { finished: boolean }>(
      'evalsPropose',
      { runPath, prompt: text, model: proposeModel, conversationId: proposeConversationId },
      (e) => {
        switch (e.kind) {
          case 'text':
            update((m) => ({ ...m, text: m.text + e.text }))
            break
          case 'thinking':
            update((m) => ({ ...m, thinking: (m.thinking ?? '') + e.text }))
            break
          case 'error':
            update((m) => ({ ...m, error: e.message }))
            break
          case 'done':
            update((m) => ({ ...m, done: true }))
            set({ proposeActive: null })
            break
          case 'proposal': {
            const key = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
            const item: ReviewItem = { key, proposal: e.proposal, valid: e.valid, reason: e.reason, checked: e.valid }
            set((st) => ({ reviewList: [...st.reviewList, item] }))
            break
          }
          default:
            break // tool_use/tool_result/viz/final_query: no UI surface in this tab
        }
      },
      'evalsCancelPropose'
    )
    set({ proposeActive: { id: handle.id, cancel: handle.cancel } })
  },

  cancelPropose: () => {
    get().proposeActive?.cancel()
    set({ proposeActive: null })
  },

  toggleReviewChecked: (key) => {
    set((st) => ({ reviewList: st.reviewList.map((r) => (r.key === key ? { ...r, checked: !r.checked } : r)) }))
  },

  clearReview: () => set({ reviewList: [] }),

  addCheckedToSet: async (runPath) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    const chosen = get().reviewList.filter((r) => r.checked && r.valid)
    if (chosen.length === 0) return
    set({ addingToSet: true })
    try {
      await rpc.call('evalsAdd', { runPath, proposals: chosen.map((r) => r.proposal) })
      set({ reviewList: get().reviewList.filter((r) => !chosen.includes(r)) })
      await get().loadQuestions(runPath)
    } finally {
      set({ addingToSet: false })
    }
  },

  removeQuestions: async (runPath, ids) => {
    const rpc = useStore.getState().rpc
    if (!rpc || ids.length === 0) return
    await rpc.call('evalsRemove', { runPath, ids })
    await get().loadQuestions(runPath)
    set({ selected: new Set() })
  }
}))
