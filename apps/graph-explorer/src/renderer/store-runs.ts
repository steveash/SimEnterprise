// The Runs view's own zustand store (docs/EXPLORER_RUNS.md §5). Kept separate
// from `./store.js` (per apps/graph-explorer's file-ownership split); it shares
// the sidecar connection with the main store by reading `rpc` off it, and for
// cross-feature actions (opening a finished run in Explore) calls
// `useStore.getState()` directly rather than being merged into it.
import { create } from 'zustand'
import { useStore } from './store.js'
import {
  elapsedSeconds as computeElapsedSeconds,
  initProjection,
  reduceEvent,
  reduceState,
  type ProjectionState
} from './jobs/projection.js'
import type {
  JobCreateResult,
  JobState,
  JobSummary,
  ProgressEvent,
  ReadRunConfigResult,
  WatchJobEvent
} from '../shared/jobs-protocol.js'

// ---- form shape (mirrors enterprise_sim.core.config.models.RunConfig) ----

export interface ProjectRow {
  key: string // UI-only row identity
  name: string
  description: string
  playbook: string // '' = default (archetype's first playbook)
  department: string // '' = default (primary department)
}

export interface RunFormState {
  company: { name: string; vertical: string; size: string; description: string }
  simulation: { period_start: string; period_end: string }
  seed: number
  output_dir: string
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
}

let rowKeyCounter = 0
const newRowKey = (): string => `row${++rowKeyCounter}`

export function emptyProjectRow(): ProjectRow {
  return { key: newRowKey(), name: '', description: '', playbook: '', department: '' }
}

export function defaultForm(): RunFormState {
  return {
    company: { name: '', vertical: 'software', size: 'startup', description: '' },
    simulation: { period_start: '', period_end: '' },
    seed: 0,
    output_dir: 'runs',
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
    live: false
  }
}

/** Build a `RunConfig` JSON document (the shape `job estimate`/`job create` accept) from the form. */
export function formToConfig(form: RunFormState): Record<string, unknown> {
  return {
    company: { ...form.company, description: form.company.description || null },
    simulation: form.simulation,
    seed: form.seed,
    output_dir: form.output_dir,
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
    }
  }
}

export interface ExtendState {
  parentRunPath: string
  parentConfig: Record<string, unknown> | null
  lineage: Record<string, unknown> | null
}

interface RunsState {
  catalog: unknown | null
  catalogLoading: boolean
  jobs: JobSummary[]
  jobsLoading: boolean

  form: RunFormState
  extend: ExtendState | null

  estimating: boolean
  estimateResult: unknown | null
  estimateError: string | null

  creating: boolean
  createError: string | null

  selectedJobDir: string | null
  projection: ProjectionState
  watching: { id: string; cancel: () => void } | null

  tomlText: string

  init: () => Promise<void>
  refreshJobs: () => Promise<void>
  loadCatalog: (refresh?: boolean) => Promise<void>

  updateForm: (patch: Partial<RunFormState>) => void
  toggleDepartment: (archetype: string) => void
  addProjectRow: () => void
  removeProjectRow: (key: string) => void
  updateProjectRow: (key: string, patch: Partial<ProjectRow>) => void
  loadFromJson: (text: string) => { ok: boolean; error?: string }
  resetForm: () => void

  estimate: () => Promise<void>
  createAndStart: () => Promise<void>

  startExtend: (runPath: string) => Promise<void>
  cancelExtend: () => void

  selectJob: (jobDir: string | null) => void
  pauseJob: (jobDir: string) => Promise<void>
  resumeJob: (jobDir: string) => Promise<void>
  cancelJob: (jobDir: string) => Promise<void>
  deleteJob: (jobDir: string) => Promise<void>
  raiseCeilingAndResume: (jobDir: string, ceilingUsd: number) => Promise<void>
  openInExplore: (runDir: string) => void
  elapsedSeconds: () => number
}

function requireRpc() {
  const rpc = useStore.getState().rpc
  if (!rpc) throw new Error('sidecar not connected')
  return rpc
}

let pollTimer: ReturnType<typeof setInterval> | null = null

export const useRunsStore = create<RunsState>((set, get) => ({
  catalog: null,
  catalogLoading: false,
  jobs: [],
  jobsLoading: false,

  form: defaultForm(),
  extend: null,

  estimating: false,
  estimateResult: null,
  estimateError: null,

  creating: false,
  createError: null,

  selectedJobDir: null,
  projection: initProjection(),
  watching: null,

  tomlText: '',

  init: async () => {
    await Promise.all([get().loadCatalog(), get().refreshJobs()])
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        void get().refreshJobs()
      }, 4000)
    }
  },

  refreshJobs: async () => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ jobsLoading: true })
    try {
      const res = await rpc.call<{ jobs: JobSummary[] }>('jobList')
      set({ jobs: res.jobs, jobsLoading: false })
    } catch {
      set({ jobsLoading: false })
    }
  },

  loadCatalog: async (refresh = false) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    set({ catalogLoading: true })
    try {
      const catalog = await rpc.call('jobCatalog', { refresh })
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

  addProjectRow: () => set((s) => ({ form: { ...s.form, projects: [...s.form.projects, emptyProjectRow()] } })),

  removeProjectRow: (key) =>
    set((s) => ({ form: { ...s.form, projects: s.form.projects.filter((p) => p.key !== key) } })),

  updateProjectRow: (key, patch) =>
    set((s) => ({
      form: { ...s.form, projects: s.form.projects.map((p) => (p.key === key ? { ...p, ...patch } : p)) }
    })),

  loadFromJson: (text) => {
    // "Load from TOML…" accepts JSON (a TOML config saved via `enterprise-sim run`
    // is not JSON, but `job estimate`/`job create` take JSON — paste the
    // equivalent JSON here; a real TOML parser is future work, noted in the UI).
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>
      const company = (parsed.company as Record<string, unknown>) ?? {}
      const simulation = (parsed.simulation as Record<string, unknown>) ?? {}
      const model = (parsed.model as Record<string, unknown>) ?? {}
      const scale = (parsed.scale as Record<string, unknown>) ?? {}
      const projects = Array.isArray(parsed.projects) ? (parsed.projects as Record<string, unknown>[]) : []
      const departments = Array.isArray(parsed.departments) ? (parsed.departments as Record<string, unknown>[]) : []
      const form: RunFormState = {
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
        output_dir: (parsed.output_dir as string) ?? 'runs',
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
        live: false
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
      const result = await rpc.call('jobEstimate', { config, live: get().form.live })
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
      const params: Record<string, unknown> = { config, live: form.live }
      if (extend) {
        params.extend = {
          parentRunDir: extend.parentRunPath,
          periodEnd: form.simulation.period_end || undefined,
          addProjects: (formToConfig(form).projects as unknown[]).slice(
            ((extend.parentConfig?.projects as unknown[]) ?? []).length
          )
        }
      }
      const created = await rpc.call<JobCreateResult>('jobCreate', params)
      if (extend) {
        // `job create --extend` derives the child's config from the parent's
        // snapshot via `extend_config()`, which only ever changes period_end /
        // added projects — model and scale are inherited from the parent
        // (docs/EXPLORER_RUNS.md §3.5). Patch them in afterward so the form's
        // model/scale section (§5 says it's editable for an extend) actually
        // takes effect, before the job ever starts rendering.
        await rpc.call('jobUpdateConfig', {
          jobDir: created.jobDir,
          patch: { model: form.model, scale: (config as Record<string, unknown>).scale }
        })
      }
      await rpc.call('jobStart', { jobDir: created.jobDir })
      set({ creating: false, extend: null })
      await get().refreshJobs()
      get().selectJob(created.jobDir)
    } catch (e) {
      set({ createError: (e as Error).message, creating: false })
    }
  },

  startExtend: async (runPath) => {
    const rpc = useStore.getState().rpc
    if (!rpc) return
    const res = await rpc.call<ReadRunConfigResult>('readRunConfig', { runPath })
    const config = res.config
    if (!config) return
    const loaded = get().loadFromJson(JSON.stringify(config))
    if (!loaded.ok) return
    set((s) => ({
      form: { ...s.form, projects: [...s.form.projects.filter((p) => p.name.trim()), emptyProjectRow()] },
      extend: { parentRunPath: runPath, parentConfig: config, lineage: res.lineage }
    }))
  },

  cancelExtend: () => set({ extend: null }),

  selectJob: (jobDir) => {
    get().watching?.cancel()
    set({ selectedJobDir: jobDir, projection: initProjection(), watching: null })
    if (!jobDir) return
    const rpc = useStore.getState().rpc
    if (!rpc) return
    const onEvent = (e: WatchJobEvent): void => {
      if (e.kind === 'progress') {
        set((s) => ({ projection: reduceEvent(s.projection, e.event as ProgressEvent) }))
      } else {
        set((s) => ({ projection: reduceState(s.projection, e.state as JobState | null) }))
        void get().refreshJobs()
      }
    }
    const watching = rpc.stream<WatchJobEvent>('watchJob', { jobDir }, onEvent, 'unwatchJob')
    set({ watching })
  },

  pauseJob: async (jobDir) => {
    await requireRpc().call('jobPause', { jobDir })
    await get().refreshJobs()
  },

  resumeJob: async (jobDir) => {
    await requireRpc().call('jobStart', { jobDir })
    await get().refreshJobs()
    if (get().selectedJobDir === jobDir) get().selectJob(jobDir) // reattach the watch
  },

  cancelJob: async (jobDir) => {
    await requireRpc().call('jobCancel', { jobDir })
    await get().refreshJobs()
  },

  deleteJob: async (jobDir) => {
    await requireRpc().call('jobDelete', { jobDir })
    if (get().selectedJobDir === jobDir) get().selectJob(null)
    await get().refreshJobs()
  },

  raiseCeilingAndResume: async (jobDir, ceilingUsd) => {
    const rpc = requireRpc()
    await rpc.call('jobUpdateConfig', { jobDir, patch: { scale: { cost_ceiling_usd: ceilingUsd } } })
    await rpc.call('jobStart', { jobDir })
    await get().refreshJobs()
    get().selectJob(jobDir)
  },

  openInExplore: (runDir) => {
    const main = useStore.getState()
    void main.loadRun(runDir).then(() => useStore.getState().setView('explore'))
  },

  elapsedSeconds: () => computeElapsedSeconds(get().projection)
}))
