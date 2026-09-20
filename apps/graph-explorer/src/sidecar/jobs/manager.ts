// JobManager: pure supervision of `enterprise-sim job …` child processes
// (docs/EXPLORER_RUNS.md §4). No simulation logic lives here — every job's
// state of record is the file `enterprise_sim.jobs` already writes
// (`<job>/state.json`); this class spawns/tails the worker, broadcasts its
// progress to watchers, and reads/patches the same on-disk files a `job` CLI
// invocation would.
import { randomBytes } from 'node:crypto'
import { mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { runJson, streamJsonl, type SpawnOptions, type StreamHandle } from '../python.js'
import type {
  JobCreateParams,
  JobCreateResult,
  JobListResult,
  JobState,
  JobSummary,
  JobUpdateConfigParams,
  ProgressEvent,
  ReadRunConfigResult,
  WatchJobEvent
} from '../../shared/jobs-protocol.js'

/** Belt-and-braces: SIGTERM a job that has not progressed 60s after a pause request. */
const PAUSE_GRACE_MS = 60_000

interface Supervised {
  handle: StreamHandle
  listeners: Set<(event: ProgressEvent) => void>
  lastProgressAt: number
  exited: boolean
  /** Resolves once the process has exited (mirrors `handle.done`, kept for readability). */
  done: Promise<number | null>
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Deep-merge `patch` onto `base`: nested plain objects merge key-by-key, anything else replaces. */
function deepMerge(base: Record<string, unknown>, patch: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base }
  for (const [k, v] of Object.entries(patch)) {
    const existing = out[k]
    out[k] = isPlainObject(existing) && isPlainObject(v) ? deepMerge(existing, v) : v
  }
  return out
}

async function readJson<T>(path: string): Promise<T | null> {
  try {
    return JSON.parse(await readFile(path, 'utf-8')) as T
  } catch {
    return null
  }
}

function abortToPromise(signal: AbortSignal): Promise<'aborted'> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve('aborted')
    signal.addEventListener('abort', () => resolve('aborted'), { once: true })
  })
}

function delay(ms: number): Promise<'timeout'> {
  return new Promise((resolve) => setTimeout(() => resolve('timeout'), ms))
}

export class JobManager {
  private supervised = new Map<string, Supervised>() // normalized job dir -> supervision state
  private catalogCache = new Map<string, unknown>() // repoRoot -> cached `job catalog` + `templates catalog`

  /** Env every spawned Python process needs so template archetypes/playbooks resolve. */
  private pluginEnv(repoRoot: string): NodeJS.ProcessEnv {
    const templatesDir = process.env.GRAPH_EXPLORER_TEMPLATES_DIR ?? join(repoRoot, 'templates')
    return { ENTERPRISE_SIM_PLUGIN_PATH: templatesDir }
  }

  private key(jobDir: string): string {
    return jobDir
  }

  // ---- catalog / estimate --------------------------------------------------

  /** `job catalog` merged with `templates catalog` (archetypes/playbooks gain template sources). */
  async catalog(repoRoot: string, opts: { refresh?: boolean } = {}): Promise<unknown> {
    if (!opts.refresh) {
      const cached = this.catalogCache.get(repoRoot)
      if (cached) return cached
    }
    const env = this.pluginEnv(repoRoot)
    const [job, templates] = await Promise.all([
      runJson<Record<string, unknown>>(repoRoot, ['job', 'catalog'], { env }),
      runJson<{ archetypes: unknown[]; playbooks: unknown[]; processes: unknown[] }>(
        repoRoot,
        ['templates', 'catalog'],
        { env }
      ).catch(() => null)
    ])
    const merged = templates
      ? { ...job, archetypes: templates.archetypes, playbooks: templates.playbooks, processes: templates.processes }
      : job
    this.catalogCache.set(repoRoot, merged)
    return merged
  }

  /** Called by a sibling feature (e.g. Templates) once a template changes, to bust the cache. */
  invalidateCatalog(repoRoot?: string): void {
    if (repoRoot) this.catalogCache.delete(repoRoot)
    else this.catalogCache.clear()
  }

  async estimate(repoRoot: string, jobsRoot: string, config: Record<string, unknown>, live = false): Promise<unknown> {
    return this.withTempConfig(jobsRoot, config, (configPath) =>
      runJson(repoRoot, ['job', 'estimate', '--config', configPath, ...(live ? ['--live'] : [])], {
        env: this.pluginEnv(repoRoot)
      })
    )
  }

  // ---- create / start / pause / cancel / delete ----------------------------

  async create(repoRoot: string, jobsRoot: string, params: JobCreateParams): Promise<JobCreateResult> {
    await mkdir(jobsRoot, { recursive: true })
    return this.withTempConfig(jobsRoot, params.config, async (configPath) => {
      const args = ['job', 'create', '--jobs-root', jobsRoot, '--config', configPath]
      if (params.live) args.push('--live')
      if (params.extend) {
        args.push('--extend', params.extend.parentRunDir)
        if (params.extend.periodEnd) args.push('--period-end', params.extend.periodEnd)
        for (const p of params.extend.addProjects ?? []) args.push('--add-project', JSON.stringify(p))
      }
      // `job create` prints `{job_id, job_dir}` (Python's own naming); translate
      // to the camelCase op result the rest of the sidecar/renderer expect.
      const raw = await runJson<{ job_id: string; job_dir: string }>(repoRoot, args, { env: this.pluginEnv(repoRoot) })
      return { jobId: raw.job_id, jobDir: raw.job_dir }
    })
  }

  /** Spawn (or, if already supervised in this process, no-op and) return `job run JOB_DIR`'s pid. */
  start(repoRoot: string, jobDir: string): { pid: number | undefined } {
    const existing = this.supervised.get(this.key(jobDir))
    if (existing && !existing.exited) return { pid: existing.handle.pid }

    const listeners = new Set<(event: ProgressEvent) => void>()
    const handle = streamJsonl(
      repoRoot,
      ['job', 'run', jobDir],
      (obj) => {
        const event = obj as ProgressEvent
        sup.lastProgressAt = Date.now()
        for (const l of listeners) l(event)
      },
      { env: this.pluginEnv(repoRoot) }
    )
    const sup: Supervised = { handle, listeners, lastProgressAt: Date.now(), exited: false, done: handle.done }
    handle.done.then(() => {
      sup.exited = true
    })
    this.supervised.set(this.key(jobDir), sup)
    return { pid: handle.pid }
  }

  /** Request a cooperative pause; SIGTERM if the worker hasn't progressed within `PAUSE_GRACE_MS`. */
  async pause(repoRoot: string, jobDir: string): Promise<void> {
    await runJson(repoRoot, ['job', 'pause', jobDir], { env: this.pluginEnv(repoRoot) })
    const sup = this.supervised.get(this.key(jobDir))
    if (!sup) return
    const requestedAt = sup.lastProgressAt
    setTimeout(() => {
      const cur = this.supervised.get(this.key(jobDir))
      if (cur && cur === sup && !cur.exited && cur.lastProgressAt <= requestedAt) {
        cur.handle.kill('SIGTERM')
      }
    }, PAUSE_GRACE_MS)
  }

  /** Pause, wait for the worker to actually stop, then mark `state.json` cancelled (job dir kept). */
  async cancel(repoRoot: string, jobDir: string): Promise<void> {
    await runJson(repoRoot, ['job', 'pause', jobDir], { env: this.pluginEnv(repoRoot) }).catch(() => {})
    const sup = this.supervised.get(this.key(jobDir))
    if (sup && !sup.exited) {
      const result = await Promise.race([sup.done.then(() => 'done' as const), delay(PAUSE_GRACE_MS)])
      if (result === 'timeout' && !sup.exited) sup.handle.kill('SIGTERM')
      await sup.done
    }
    const state = await this.statusOf(jobDir)
    if (state && state.status !== 'done') {
      state.status = 'cancelled'
      await this.writeStateFile(jobDir, state)
    }
  }

  /** Remove the job directory (not the run it may have produced). Refuses while supervised & alive. */
  async delete(jobDir: string): Promise<void> {
    const sup = this.supervised.get(this.key(jobDir))
    if (sup && !sup.exited) {
      throw new Error('cannot delete a running job; pause or cancel it first')
    }
    await rm(jobDir, { recursive: true, force: true })
  }

  // ---- list / status --------------------------------------------------------

  async list(jobsRoot: string): Promise<JobListResult> {
    if (!existsSync(jobsRoot)) return { jobs: [] }
    const entries = await readdir(jobsRoot, { withFileTypes: true })
    const jobs: JobSummary[] = []
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const jobDir = join(jobsRoot, entry.name)
      const state = await readJson<JobState>(join(jobDir, 'state.json'))
      if (!state) continue
      const job = (await readJson<Record<string, unknown>>(join(jobDir, 'job.json'))) ?? {}
      jobs.push({
        job_id: entry.name,
        job_dir: jobDir,
        state,
        job: {
          kind: job.kind as 'new' | 'extend' | undefined,
          live: job.live as boolean | undefined,
          created_at: job.created_at as string | undefined,
          parent_run_dir: job.parent_run_dir as string | null | undefined
        },
        alive: state.status === 'running' ? isAlive(state.pid) : false
      })
    }
    jobs.sort((a, b) => a.job_id.localeCompare(b.job_id))
    return { jobs }
  }

  async statusOf(jobDir: string): Promise<JobState | null> {
    return readJson<JobState>(join(jobDir, 'state.json'))
  }

  private async writeStateFile(jobDir: string, state: JobState): Promise<void> {
    const path = join(jobDir, 'state.json')
    const tmp = `${path}.tmp`
    state.updated_at = new Date().toISOString()
    await writeFile(tmp, JSON.stringify(state, null, 2) + '\n', 'utf-8')
    await rename(tmp, path)
  }

  // ---- watch / unwatch --------------------------------------------------------

  /**
   * Replay `<job>/progress.jsonl`, then (if this manager is actively supervising
   * `jobDir`) forward live events until the process exits or `signal` aborts.
   * Resolves immediately after replay when the job isn't running under this
   * process. Always emits a final `{kind:'state'}` when the job actually exits;
   * an explicit `unwatchJob` (abort) resolves without one.
   */
  async watch(jobDir: string, onEvent: (e: WatchJobEvent) => void, signal: AbortSignal): Promise<void> {
    const progressPath = join(jobDir, 'progress.jsonl')
    if (existsSync(progressPath)) {
      const text = await readFile(progressPath, 'utf-8')
      for (const line of text.split('\n')) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try {
          onEvent({ kind: 'progress', event: JSON.parse(trimmed) as ProgressEvent })
        } catch {
          /* skip a malformed line */
        }
      }
    }

    const sup = this.supervised.get(this.key(jobDir))
    if (!sup || sup.exited) {
      onEvent({ kind: 'state', state: await this.statusOf(jobDir) })
      return
    }

    const listener = (event: ProgressEvent): void => onEvent({ kind: 'progress', event })
    sup.listeners.add(listener)
    try {
      const result = await Promise.race([sup.done.then(() => 'done' as const), abortToPromise(signal)])
      if (result === 'done') {
        onEvent({ kind: 'state', state: await this.statusOf(jobDir) })
      }
    } finally {
      sup.listeners.delete(listener)
    }
  }

  // ---- config read/patch --------------------------------------------------------

  async readRunConfig(runPath: string): Promise<ReadRunConfigResult> {
    const [config, lineage] = await Promise.all([
      readJson<Record<string, unknown>>(join(runPath, 'config.snapshot.json')),
      readJson<Record<string, unknown>>(join(runPath, 'lineage.json'))
    ])
    return { config, lineage }
  }

  async updateConfig(params: JobUpdateConfigParams): Promise<{ config: Record<string, unknown> }> {
    const path = join(params.jobDir, 'config.json')
    const current = (await readJson<Record<string, unknown>>(path)) ?? {}
    const merged = deepMerge(current, params.patch)
    await writeFile(path, JSON.stringify(merged, null, 2) + '\n', 'utf-8')
    return { config: merged }
  }

  // ---- helpers --------------------------------------------------------

  private async withTempConfig<T>(
    jobsRoot: string,
    config: Record<string, unknown>,
    fn: (configPath: string) => Promise<T>
  ): Promise<T> {
    const tmpRoot = join(jobsRoot, '.tmp')
    await mkdir(tmpRoot, { recursive: true })
    const dir = await mkdtemp(join(tmpRoot, 'cfg-'))
    const configPath = join(dir, `${randomBytes(4).toString('hex')}.json`)
    await writeFile(configPath, JSON.stringify(config), 'utf-8')
    try {
      return await fn(configPath)
    } finally {
      await rm(dir, { recursive: true, force: true }).catch(() => {})
    }
  }
}

/** Best-effort liveness check for a recorded pid (POSIX signal 0; mirrors `jobs/state.py::is_alive`). */
export function isAlive(pid: number | null): boolean {
  if (pid === null || pid === undefined) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

// Re-exported so tests can construct spawn options without importing python.js directly.
export type { SpawnOptions }
