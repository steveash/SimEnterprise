// The Worlds view's own zustand store (docs/EXPLORER_RUNS.md §5). Kept separate
// from `./store.js` (per apps/graph-explorer's file-ownership split); it shares
// the sidecar connection with the main store by reading `rpc` off it, and the
// world LIST itself lives in the main store (`useStore().worlds`) so the topbar
// picker and this view always render the same thing. Cross-feature actions
// (opening a world in Explore) call `useStore.getState()` directly.
import { create } from 'zustand'
import { useStore } from './store.js'
import {
  elapsedSeconds as computeElapsedSeconds,
  initProjection,
  reduceEvent,
  reduceState,
  type ProjectionState
} from './worlds/projection.js'
import type {
  JobState,
  ProgressEvent,
  ReadWorldConfigResult,
  WatchWorldEvent,
  WorldCreateResult,
  WorldExtendResult
} from '../shared/worlds-protocol.js'

// ---- form shape (mirrors enterprise_sim.core.config.models.WorldConfig) ----

export interface ProjectRow {
  key: string // UI-only row identity
  name: string
  description: string
  playbook: string // '' = default (archetype's first playbook)
  department: string // '' = default (primary department)
}

export interface WorldFormState {
  company: { name: string; vertical: string; size: string; description: string }
  simulation: { period_start: string; period_end: string }
  seed: number
  worlds_root: string
  departments: string[] // archetype names, ordered; first = primary
  projects: ProjectRow[]
  model: { backend: string; name: string; realism: number }
  scale: {
    max_concurrency: number
    cost_ceiling_usd: number | null
    cache_dir: string
    est_input_tokens_per_artifact: number
    est_cached_input_tokens_per_artifact: number
    est_output_tokens_per_artifact: number
  }
  live: boolean
  /** The structured-data half of the world (WorldConfig.data). */
  data: {
    scenarios: string[]
    authoring: 'template' | 'llm'
    scale_factor: number
    iterations: number
  }
}

let rowKeyCounter = 0
const newRowKey = (): string => `row${++rowKeyCounter}`

export function emptyProjectRow(): ProjectRow {
  return { key: newRowKey(), name: '', description: '', playbook: '', department: '' }
}

export function defaultForm(): WorldFormState {
  return {
    company: { name: '', vertical: 'software', size: 'startup', description: '' },
    simulation: { period_start: '', period_end: '' },
    seed: 0,
    worlds_root: 'worlds',
    departments: [],
    projects: [emptyProjectRow()],
    model: { backend: 'anthropic_api', name: 'claude-opus-4-8', realism: 0.7 },
    scale: {
      max_concurrency: 8,
      cost_ceiling_usd: null,
      cache_dir: '',
      est_input_tokens_per_artifact: 1200,
      est_cached_input_tokens_per_artifact: 0,
      est_output_tokens_per_artifact: 600
    },
    live: false,
    data: { scenarios: [], authoring: 'template', scale_factor: 1, iterations: 3 }
  }
}

/** Build a `WorldConfig` JSON document (the shape `world estimate`/`world create` accept) from the form. */
export function formToConfig(form: WorldFormState): Record<string, unknown> {
  return {
    company: { ...form.company, description: form.company.description || null },
    simulation: form.simulation,
    seed: form.seed,
    worlds_root: form.worlds_root,
    projects: form.projects
      .filter((p) => p.name.trim())
      .map((p) => ({
        name: p.name,
        description: p.description || null,
        playbook: p.playbook || null,
        department: p.department || null
      })),
    departments: form.departments.map((archetype) => ({ archetype })),
    model: form.model,
    scale: {
      ...form.scale,
      cache_dir: form.scale.cache_dir || null
    },
    data: {
      scenarios: form.data.scenarios,
      authoring: form.data.authoring,
      scale_factor: form.data.scale_factor,
      iterations: form.data.iterations
    }
  }
}

export interface ExtendState {
  parentWorldDir: string
  parentConfig: Record<string, unknown> | null
  lineage: ReadWorldConfigResult['lineage']
  history: ReadWorldConfigResult['history']
}

interface WorldsState {
  catalog: unknown | null
  catalogLoading: boolean

  form: WorldFormState
  extend: ExtendState | null

  estimating: boolean
  estimateResult: unknown | null
  estimateError: string | null

  creating: boolean
  createError: string | null

  selectedWorldDir: string | null
  projection: ProjectionState
  watching: { id: string; cancel: () => void } | null

  tomlText: string

  init: () => Promise<void>
  loadCatalog: (refresh?: boolean) => Promise<void>

  updateForm: (patch: Partial<WorldFormState>) => void
  toggleDepartment: (archetype: string) => void
  toggleDataScenario: (name: string) => void
  addProjectRow: () => void
  removeProjectRow: (key: string) => void
  updateProjectRow: (key: string, patch: Partial<ProjectRow>) => void
  loadFromJson: (text: string) => { ok: boolean; error?: string }
  resetForm: () => void

  estimate: () => Promise<void>
  createAndStart: () => Promise<void>

  startExtend: (worldDir: string) => Promise<void>
  cancelExtend: () => void

  selectWorld: (worldDir: string | null) => void
  pauseWorld: (worldDir: string) => Promise<void>
  resumeWorld: (worldDir: string) => Promise<void>
  cancelWorld: (worldDir: string) => Promise<void>
  deleteWorld: (worldDir: string) => Promise<void>
  raiseCeilingAndResume: (worldDir: string, ceilingUsd: number) => Promise<void>
  openInExplore: (worldDir: string) => void
  elapsedSeconds: () => number
}

function requireRpc() {
  const rpc = useStore.getState().rpc
  if (!rpc) throw new Error('sidecar not connected')
  return rpc
}

const refreshWorlds = (): Promise<void> => useStore.getState().refreshWorlds()

let pollTimer: ReturnType<typeof setInterval> | null = null

export const useWorldsStore = create<WorldsState>((set, get) => ({
  catalog: null,
  catalogLoading: false,

  form: defaultForm(),
  extend: null,

  estimating: false,
  estimateResult: null,
  estimateError: null,

  creating: false,
  createError: null,

  selectedWorldDir: null,
  projection: initProjection(),
  watching: null,

  tomlText: '',

  init: async () => {
    await Promise.all([get().loadCatalog(), refreshWorlds()])
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        void refreshWorlds()
      }, 4000)
    }
  },

  loadCatalog: async (refresh = false) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ catalogLoading: true })
    try {
      const catalog = await rpc.call('worldCatalog', { refresh })
      set({ catalog, catalogLoading: false })
    } catch {
      set({ catalogLoading: false })
    }
  },

  updateForm: (patch) => set((s) => ({ form: { ...s.form, ...patch } })),

  toggleDepartment: (archetype) =>
    set((s) => {
      const has = s.form.departments.includes(archetype)
      const departments = has ? s.form.departments.filter((d) => d !== archetype) : [...s.form.departments, archetype]
      return { form: { ...s.form, departments } }
    }),

  toggleDataScenario: (name) =>
    set((s) => {
      const has = s.form.data.scenarios.includes(name)
      const scenarios = has ? s.form.data.scenarios.filter((d) => d !== name) : [...s.form.data.scenarios, name]
      return { form: { ...s.form, data: { ...s.form.data, scenarios } } }
    }),

  addProjectRow: () => set((s) => ({ form: { ...s.form, projects: [...s.form.projects, emptyProjectRow()] } })),

  removeProjectRow: (key) =>
    set((s) => ({ form: { ...s.form, projects: s.form.projects.filter((p) => p.key !== key) } })),

  updateProjectRow: (key, patch) =>
    set((s) => ({
      form: { ...s.form, projects: s.form.projects.map((p) => (p.key === key ? { ...p, ...patch } : p)) }
    })),

  loadFromJson: (text) => {
    // "Load from TOML…" accepts JSON (a TOML config is not JSON, but `world
    // estimate`/`world create` take JSON — paste the equivalent JSON here; a
    // real TOML parser is future work, noted in the UI).
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>
      const company = (parsed.company as Record<string, unknown>) ?? {}
      const simulation = (parsed.simulation as Record<string, unknown>) ?? {}
      const model = (parsed.model as Record<string, unknown>) ?? {}
      const scale = (parsed.scale as Record<string, unknown>) ?? {}
      const projects = Array.isArray(parsed.projects) ? (parsed.projects as Record<string, unknown>[]) : []
      const departments = Array.isArray(parsed.departments) ? (parsed.departments as Record<string, unknown>[]) : []
      const data = (parsed.data as Record<string, unknown>) ?? {}
      const form: WorldFormState = {
        company: {
          name: (company.name as string) ?? '',
          vertical: (company.vertical as string) ?? 'software',
          size: (company.size as string) ?? 'startup',
          description: (company.description as string) ?? ''
        },
        simulation: {
          period_start: (simulation.period_start as string) ?? '',
          period_end: (simulation.period_end as string) ?? ''
        },
        seed: typeof parsed.seed === 'number' ? parsed.seed : 0,
        worlds_root: (parsed.worlds_root as string) ?? 'worlds',
        departments: departments.map((d) => String(d.archetype ?? '')).filter(Boolean),
        projects: projects.length
          ? projects.map((p) => ({
              key: newRowKey(),
              name: (p.name as string) ?? '',
              description: (p.description as string) ?? '',
              playbook: (p.playbook as string) ?? '',
              department: (p.department as string) ?? ''
            }))
          : [emptyProjectRow()],
        model: {
          backend: (model.backend as string) ?? 'anthropic_api',
          name: (model.name as string) ?? 'claude-opus-4-8',
          realism: typeof model.realism === 'number' ? model.realism : 0.7
        },
        scale: {
          max_concurrency: (scale.max_concurrency as number) ?? 8,
          cost_ceiling_usd: (scale.cost_ceiling_usd as number) ?? null,
          cache_dir: (scale.cache_dir as string) ?? '',
          est_input_tokens_per_artifact: (scale.est_input_tokens_per_artifact as number) ?? 1200,
          est_cached_input_tokens_per_artifact: (scale.est_cached_input_tokens_per_artifact as number) ?? 0,
          est_output_tokens_per_artifact: (scale.est_output_tokens_per_artifact as number) ?? 600
        },
        live: false,
        data: {
          scenarios: Array.isArray(data.scenarios) ? (data.scenarios as string[]) : [],
          authoring: data.authoring === 'llm' ? 'llm' : 'template',
          scale_factor: typeof data.scale_factor === 'number' ? data.scale_factor : 1,
          iterations: typeof data.iterations === 'number' ? data.iterations : 3
        }
      }
      set({ form, tomlText: text })
      return { ok: true }
    } catch (e) {
      return { ok: false, error: (e as Error).message }
    }
  },

  resetForm: () => set({ form: defaultForm(), estimateResult: null, estimateError: null, extend: null }),

  estimate: async () => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ estimating: true, estimateError: null })
    try {
      const config = formToConfig(get().form)
      const result = await rpc.call('worldEstimate', { config, live: get().form.live })
      set({ estimateResult: result, estimating: false })
    } catch (e) {
      set({ estimateError: (e as Error).message, estimating: false, estimateResult: null })
    }
  },

  createAndStart: async () => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ creating: true, createError: null })
    try {
      const { form, extend } = get()
      const config = formToConfig(form)
      let worldDir: string
      if (extend) {
        // An extend grows the SAME world: `world extend` adopts a later
        // period_end / added projects in place (docs/EXPLORER_RUNS.md §3.5).
        // The model/scale knobs and the structured-data plan are then patched in
        // with `world configure` (none of them change the world's identity). A
        // data-only extend skips `world extend` — the rebuild re-renders nothing
        // (all cache hits) and adds the new data scenarios.
        const parentSim = (extend.parentConfig?.simulation as { period_end?: string } | undefined) ?? {}
        const added = (config.projects as unknown[]).slice(((extend.parentConfig?.projects as unknown[]) ?? []).length)
        const later = Boolean(form.simulation.period_end) && form.simulation.period_end !== parentSim.period_end
        worldDir = extend.parentWorldDir
        if (later || added.length > 0) {
          const extended = await rpc.call<WorldExtendResult>('worldExtend', {
            worldDir,
            periodEnd: later ? form.simulation.period_end : undefined,
            addProjects: added,
            live: form.live
          })
          worldDir = extended.worldDir
        }
        await rpc.call('worldConfigure', {
          worldDir,
          patch: {
            model: form.model,
            scale: (config as Record<string, unknown>).scale,
            data: (config as Record<string, unknown>).data
          }
        })
      } else {
        const created = await rpc.call<WorldCreateResult>('worldCreate', { config, live: form.live })
        worldDir = created.worldDir
      }
      await rpc.call('worldStart', { worldDir })
      set({ creating: false, extend: null })
      await refreshWorlds()
      get().selectWorld(worldDir)
    } catch (e) {
      set({ createError: (e as Error).message, creating: false })
    }
  },

  startExtend: async (worldDir) => {
    const rpc = useStore.getState().rpc
    if (!rpc) throw new Error('not connected to the graph engine (sidecar)')
    const res = await rpc.call<ReadWorldConfigResult>('readWorldConfig', { worldDir })
    const config = res.config
    if (!config) throw new Error(`${worldDir} has no config in its .world file`)
    const loaded = get().loadFromJson(JSON.stringify(config))
    if (!loaded.ok) throw new Error(`could not read the world's config: ${loaded.error ?? 'invalid JSON'}`)
    set((s) => ({
      form: { ...s.form, projects: [...s.form.projects.filter((p) => p.name.trim()), emptyProjectRow()] },
      extend: { parentWorldDir: worldDir, parentConfig: config, lineage: res.lineage, history: res.history }
    }))
  },

  cancelExtend: () => set({ extend: null }),

  selectWorld: (worldDir) => {
    get().watching?.cancel()
    set({ selectedWorldDir: worldDir, projection: initProjection(), watching: null })
    if (!worldDir) return
    const rpc = useStore.getState().rpc
    if (!rpc) return
    const onEvent = (e: WatchWorldEvent): void => {
      if (e.kind === 'progress') {
        set((s) => ({ projection: reduceEvent(s.projection, e.event as ProgressEvent) }))
      } else {
        set((s) => ({ projection: reduceState(s.projection, e.state as JobState | null) }))
        void refreshWorlds()
      }
    }
    const watching = rpc.stream<WatchWorldEvent>('watchWorld', { worldDir }, onEvent, 'unwatchWorld')
    set({ watching })
  },

  pauseWorld: async (worldDir) => {
    await requireRpc().call('worldPause', { worldDir })
    await refreshWorlds()
  },

  resumeWorld: async (worldDir) => {
    await requireRpc().call('worldStart', { worldDir })
    await refreshWorlds()
    if (get().selectedWorldDir === worldDir) get().selectWorld(worldDir) // reattach the watch
  },

  cancelWorld: async (worldDir) => {
    await requireRpc().call('worldCancel', { worldDir })
    await refreshWorlds()
  },

  deleteWorld: async (worldDir) => {
    await requireRpc().call('worldDelete', { worldDir })
    if (get().selectedWorldDir === worldDir) get().selectWorld(null)
    await refreshWorlds()
  },

  raiseCeilingAndResume: async (worldDir, ceilingUsd) => {
    const rpc = requireRpc()
    await rpc.call('worldConfigure', { worldDir, patch: { scale: { cost_ceiling_usd: ceilingUsd } } })
    await rpc.call('worldStart', { worldDir })
    await refreshWorlds()
    get().selectWorld(worldDir)
  },

  openInExplore: (worldDir) => {
    // Switch first so the Explore loading overlay is what the user sees while
    // the (many-second) graph-engine build runs, instead of a frozen Worlds tab.
    const main = useStore.getState()
    main.setView('explore')
    void main.loadWorld(worldDir)
  },

  elapsedSeconds: () => computeElapsedSeconds(get().projection)
}))
