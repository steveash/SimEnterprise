// WorldManager: pure supervision of `enterprise-sim world …` child processes
// (docs/EXPLORER_RUNS.md §4). No simulation logic lives here — every world's
// state of record is its `.world` file, which only the Python side writes;
// this class spawns/tails the worker, broadcasts its progress to watchers, and
// reads the same on-disk files a `world` CLI invocation would.
import { randomBytes } from 'node:crypto'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { runJson, streamJsonl, type SpawnOptions, type StreamHandle } from '../python.js'
import { PROGRESS_LOG, discoverWorlds, readWorldFile, summarizeWorld } from './discover.js'
import type {
  JobState,
  ProgressEvent,
  ReadWorldConfigResult,
  WatchWorldEvent,
  WorldConfigureParams,
  WorldCreateParams,
  WorldCreateResult,
  WorldExtendParams,
  WorldExtendResult,
  WorldListResult,
  WorldSummary
} from '../../shared/worlds-protocol.js'

/** Belt-and-braces: SIGTERM a build that has not progressed 60s after a pause request. */
const PAUSE_GRACE_MS = 60_000

interface Supervised {
  handle: StreamHandle
  listeners: Set<(event: ProgressEvent) => void>
  lastProgressAt: number
  exited: boolean
  /** Resolves once the process has exited (mirrors `handle.done`, kept for readability). */
  done: Promise<number | null>
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

export class WorldManager {
  private supervised = new Map<string, Supervised>() // world dir -> supervision state
  private catalogCache = new Map<string, unknown>() // repoRoot -> cached `world catalog` + `templates catalog`

  /** Env every spawned Python process needs so template archetypes/playbooks resolve. */
  private pluginEnv(repoRoot: string): NodeJS.ProcessEnv {
    const templatesDir = process.env.GRAPH_EXPLORER_TEMPLATES_DIR ?? join(repoRoot, 'templates')
    return { ENTERPRISE_SIM_PLUGIN_PATH: templatesDir }
  }

  // ---- catalog / estimate --------------------------------------------------

  /** `world catalog` merged with `templates catalog` (archetypes/playbooks gain template sources). */
  async catalog(repoRoot: string, opts: { refresh?: boolean } = {}): Promise<unknown> {
    if (!opts.refresh) {
      const cached = this.catalogCache.get(repoRoot)
      if (cached) return cached
    }
    const env = this.pluginEnv(repoRoot)
    const [world, templates] = await Promise.all([
      runJson<Record<string, unknown>>(repoRoot, ['world', 'catalog'], { env }),
      runJson<{ archetypes: unknown[]; playbooks: unknown[]; processes: unknown[]; data_scenarios?: unknown[] }>(
        repoRoot,
        ['templates', 'catalog'],
        { env }
      ).catch(() => null)
    ])
    // The templates catalog attributes every entry to its template; prefer it.
    const merged = templates
      ? {
          ...world,
          archetypes: templates.archetypes,
          playbooks: templates.playbooks,
          processes: templates.processes,
          data_scenarios: templates.data_scenarios ?? world.data_scenarios
        }
      : world
    this.catalogCache.set(repoRoot, merged)
    return merged
  }

  /** Called by a sibling feature (e.g. Templates) once a template changes, to bust the cache. */
  invalidateCatalog(repoRoot?: string): void {
    if (repoRoot) this.catalogCache.delete(repoRoot)
    else this.catalogCache.clear()
  }

  async estimate(repoRoot: string, config: Record<string, unknown>, live = false): Promise<unknown> {
    return this.withTempConfig(config, (configPath) =>
      runJson(repoRoot, ['world', 'estimate', '--config', configPath, ...(live ? ['--live'] : [])], {
        env: this.pluginEnv(repoRoot)
      })
    )
  }

  // ---- create / extend / start / pause / cancel / delete ----------------------

  /** `world create --no-run`: mint `<worldsRoot>/<world-id>/.world` without building it. */
  async create(repoRoot: string, worldsRoot: string, params: WorldCreateParams): Promise<WorldCreateResult> {
    return this.withTempConfig(params.config, async (configPath) => {
      const args = ['world', 'create', '--config', configPath, '--worlds-root', worldsRoot, '--no-run']
      if (params.live) args.push('--live')
      // `world create --no-run` prints `{world_id, world_dir}` (Python's own naming);
      // translate to the camelCase op result the rest of the sidecar/renderer expect.
      const raw = await runJson<{ world_id: string; world_dir: string }>(repoRoot, args, { env: this.pluginEnv(repoRoot) })
      return { worldId: raw.world_id, worldDir: raw.world_dir }
    })
  }

  /** `world extend --no-run`: adopt a later period_end / more projects into the SAME world. */
  async extend(repoRoot: string, params: WorldExtendParams): Promise<WorldExtendResult> {
    const args = ['world', 'extend', params.worldDir, '--no-run']
    if (params.periodEnd) args.push('--period-end', params.periodEnd)
    for (const p of params.addProjects ?? []) args.push('--add-project', JSON.stringify(p))
    if (params.live) args.push('--live')
    const raw = await runJson<{ world_id: string; world_dir: string; changes: Record<string, unknown> }>(repoRoot, args, {
      env: this.pluginEnv(repoRoot)
    })
    return { worldId: raw.world_id, worldDir: raw.world_dir, changes: raw.changes }
  }

  /** Spawn (or, if already supervised in this process, no-op and) return `world run WORLD_DIR`'s pid. */
  start(repoRoot: string, worldDir: string): { pid: number | undefined } {
    const existing = this.supervised.get(worldDir)
    if (existing && !existing.exited) return { pid: existing.handle.pid }

    const listeners = new Set<(event: ProgressEvent) => void>()
    const handle = streamJsonl(
      repoRoot,
      ['world', 'run', worldDir],
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
    this.supervised.set(worldDir, sup)
    return { pid: handle.pid }
  }

  /** Request a cooperative pause; SIGTERM if the worker hasn't progressed within `PAUSE_GRACE_MS`. */
  async pause(repoRoot: string, worldDir: string): Promise<void> {
    await runJson(repoRoot, ['world', 'pause', worldDir], { env: this.pluginEnv(repoRoot) }).catch(() => {})
    const sup = this.supervised.get(worldDir)
    if (!sup) return
    const requestedAt = sup.lastProgressAt
    setTimeout(() => {
      const cur = this.supervised.get(worldDir)
      if (cur && cur === sup && !cur.exited && cur.lastProgressAt <= requestedAt) {
        cur.handle.kill('SIGTERM')
      }
    }, PAUSE_GRACE_MS)
  }

  /** Pause, wait for the worker to actually stop, then `world cancel` (the world is kept). */
  async cancel(repoRoot: string, worldDir: string): Promise<void> {
    await runJson(repoRoot, ['world', 'pause', worldDir], { env: this.pluginEnv(repoRoot) }).catch(() => {})
    const sup = this.supervised.get(worldDir)
    if (sup && !sup.exited) {
      const result = await Promise.race([sup.done.then(() => 'done' as const), delay(PAUSE_GRACE_MS)])
      if (result === 'timeout' && !sup.exited) sup.handle.kill('SIGTERM')
      await sup.done
    }
    await runJson(repoRoot, ['world', 'cancel', worldDir], { env: this.pluginEnv(repoRoot) })
  }

  /** Remove the whole world directory. Refuses while supervised & alive. */
  async delete(worldDir: string): Promise<void> {
    const sup = this.supervised.get(worldDir)
    if (sup && !sup.exited) {
      throw new Error('cannot delete a world that is being built; pause or cancel it first')
    }
    await rm(worldDir, { recursive: true, force: true })
  }

  // ---- list / status --------------------------------------------------------

  /** Every world under `worldsRoot`, straight off the `.world` files (no subprocess). */
  list(worldsRoot: string): WorldListResult {
    return { worlds: discoverWorlds(worldsRoot) }
  }

  summaryOf(worldDir: string): WorldSummary | null {
    return summarizeWorld(worldDir)
  }

  /** The world's job ledger (`.world` → `job`), or null when it holds no `.world`. */
  statusOf(worldDir: string): JobState | null {
    return summarizeWorld(worldDir)?.job ?? null
  }

  // ---- watch / unwatch --------------------------------------------------------

  /**
   * Replay `<world>/.jobs/progress.jsonl`, then (if this manager is actively
   * supervising `worldDir`) forward live events until the process exits or
   * `signal` aborts. Resolves immediately after replay when the world isn't
   * being built under this process. Always emits a final `{kind:'state'}` when
   * the build actually exits; an explicit `unwatchWorld` (abort) resolves without one.
   */
  async watch(worldDir: string, onEvent: (e: WatchWorldEvent) => void, signal: AbortSignal): Promise<void> {
    const progressPath = join(worldDir, PROGRESS_LOG)
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

    const sup = this.supervised.get(worldDir)
    if (!sup || sup.exited) {
      onEvent({ kind: 'state', state: this.statusOf(worldDir) })
      return
    }

    const listener = (event: ProgressEvent): void => onEvent({ kind: 'progress', event })
    sup.listeners.add(listener)
    try {
      const result = await Promise.race([sup.done.then(() => 'done' as const), abortToPromise(signal)])
      if (result === 'done') {
        onEvent({ kind: 'state', state: this.statusOf(worldDir) })
      }
    } finally {
      sup.listeners.delete(listener)
    }
  }

  // ---- config read/patch --------------------------------------------------------

  /** The world's current config + lineage + history, from its `.world` (feeds the Extend form). */
  readConfig(worldDir: string): ReadWorldConfigResult {
    const world = readWorldFile(worldDir)
    const config = world?.config
    const lineage = world?.lineage
    const history = Array.isArray(world?.history) ? (world.history as ReadWorldConfigResult['history']) : []
    return {
      config: typeof config === 'object' && config !== null ? (config as Record<string, unknown>) : null,
      lineage: typeof lineage === 'object' && lineage !== null ? (lineage as ReadWorldConfigResult['lineage']) : null,
      history
    }
  }

  /** `world configure --patch`: deep-merge a patch into the world's config (validated by Python). */
  async configure(repoRoot: string, params: WorldConfigureParams): Promise<{ config: Record<string, unknown> }> {
    const raw = await runJson<{ world_id: string; config: Record<string, unknown> }>(
      repoRoot,
      ['world', 'configure', params.worldDir, '--patch', JSON.stringify(params.patch)],
      { env: this.pluginEnv(repoRoot) }
    )
    return { config: raw.config }
  }

  // ---- helpers --------------------------------------------------------

  private async withTempConfig<T>(config: Record<string, unknown>, fn: (configPath: string) => Promise<T>): Promise<T> {
    const dir = await mkdtemp(join(tmpdir(), 'graph-explorer-cfg-'))
    const configPath = join(dir, `${randomBytes(4).toString('hex')}.json`)
    await writeFile(configPath, JSON.stringify(config), 'utf-8')
    try {
      return await fn(configPath)
    } finally {
      await rm(dir, { recursive: true, force: true }).catch(() => {})
    }
  }
}

export { isAlive } from './discover.js'
// Re-exported so tests can construct spawn options without importing python.js directly.
export type { SpawnOptions }
