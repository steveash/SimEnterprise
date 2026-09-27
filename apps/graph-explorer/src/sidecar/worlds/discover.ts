// World discovery straight off the filesystem: a world is a directory holding a
// `.world` file (enterprise_sim.worlds). This is the TypeScript twin of
// `enterprise_sim.worlds.summary.summarize_world` / `discover_worlds`, kept in
// the sidecar so the renderer's 4-second poll never pays a Python subprocess.
// The `.world` is the single source of truth; nothing here infers a world from
// `kg/` or any other side effect of a build.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import type { DataScenarioSummary, JobState, WorldStatus, WorldSummary } from '../../shared/worlds-protocol.js'

export const WORLD_FILE = '.world'
export const PROGRESS_LOG = join('.jobs', 'progress.jsonl')
export const CONTROL_FILE = join('.jobs', 'control.json')

/** `true` iff `dir` holds a `.world` file — the one and only world test. */
export function isWorldDir(dir: string): boolean {
  return existsSync(join(dir, WORLD_FILE))
}

/** Best-effort liveness check for a recorded pid (POSIX signal 0; mirrors `jobs/state.py::is_alive`). */
export function isAlive(pid: number | null | undefined): boolean {
  if (pid === null || pid === undefined) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

/** The parsed `.world` document, or null when absent/unreadable. */
export function readWorldFile(dir: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(readFileSync(join(dir, WORLD_FILE), 'utf-8')) as unknown
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null
  } catch {
    return null
  }
}

function obj(v: unknown): Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function num(v: unknown, fallback = 0): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}

function jobState(raw: unknown): JobState {
  const j = obj(raw)
  return {
    status: str(j.status, 'created') as JobState['status'],
    pid: typeof j.pid === 'number' ? j.pid : null,
    phase: typeof j.phase === 'string' ? j.phase : null,
    artifacts_done: num(j.artifacts_done),
    artifacts_total: num(j.artifacts_total),
    artifacts_cached: num(j.artifacts_cached),
    estimate: typeof j.estimate === 'object' && j.estimate !== null ? (j.estimate as Record<string, unknown>) : null,
    cost_usd_total: num(j.cost_usd_total),
    cost_usd_segment: num(j.cost_usd_segment),
    usage_total: obj(j.usage_total) as Record<string, number>,
    segments: Array.isArray(j.segments) ? (j.segments as JobState['segments']) : [],
    started_at: typeof j.started_at === 'string' ? j.started_at : null,
    updated_at: typeof j.updated_at === 'string' ? j.updated_at : null,
    finished_at: typeof j.finished_at === 'string' ? j.finished_at : null,
    error: typeof j.error === 'object' && j.error !== null ? (j.error as JobState['error']) : null
  }
}

/** Mirrors `summary.py::_data_summary`: the headline of one structured-data scenario. */
function dataSummary(raw: unknown): DataScenarioSummary {
  const entry = obj(raw)
  const rows = obj(entry.rows_by_table)
  const iterations = Array.isArray(entry.iterations) ? entry.iterations.length : 0
  return {
    title: typeof entry.title === 'string' ? entry.title : null,
    rows: Object.values(rows).reduce<number>((a, n) => a + num(n), 0),
    tables: Object.keys(rows).length,
    questions: obj(entry.questions) as DataScenarioSummary['questions'],
    iterations,
    llm_cost_usd: num(entry.llm_cost_usd),
    created_at: typeof entry.created_at === 'string' ? entry.created_at : null
  }
}

/** Summarize the world at `dir` (mirrors `summarize_world`), or null if it holds no `.world`. */
export function summarizeWorld(dir: string): WorldSummary | null {
  const world = readWorldFile(dir)
  if (!world) return null
  const job = jobState(world.job)
  const alive = job.status === 'running' ? isAlive(job.pid) : false
  // A "running" job whose process is gone was interrupted (killed, machine
  // rebooted, app closed hard): resumable, but not actually running.
  const status: WorldStatus = job.status === 'running' && !alive ? 'interrupted' : job.status
  const data = obj(world.data)
  const history = Array.isArray(world.history) ? world.history.map((h) => obj(h)) : []
  const lineage = world.lineage
  return {
    world_id: str(world.world_id, dir.split('/').pop() ?? dir),
    world_dir: dir,
    schema_version: str(world.schema_version),
    company: obj(world.company) as WorldSummary['company'],
    seed: num(world.seed),
    window: obj(world.window) as WorldSummary['window'],
    counts: obj(world.counts) as WorldSummary['counts'],
    render_backend: str(world.render_backend, 'fake'),
    live: world.live === true,
    config_digest: str(world.config_digest),
    created_at: str(world.created_at),
    updated_at: str(world.updated_at),
    status,
    alive,
    job,
    has_kg: existsSync(join(dir, 'kg', 'nodes.jsonl')),
    data: Object.fromEntries(
      Object.keys(data)
        .sort()
        .map((name) => [name, dataSummary(data[name])])
    ),
    evals: obj(world.evals) as WorldSummary['evals'],
    history: history.map((h) => ({
      kind: str(h.kind),
      at: typeof h.at === 'string' ? h.at : null,
      changes: typeof h.changes === 'object' && h.changes !== null ? (h.changes as Record<string, unknown>) : null
    })),
    lineage: typeof lineage === 'object' && lineage !== null ? (lineage as WorldSummary['lineage']) : null
  }
}

/**
 * Every world under `root` (depth-first, ≤ `maxDepth`, sorted by path). A
 * directory holding a `.world` is listed and not descended into; dot-directories
 * (a world's own `.cache` / `.jobs`, and anything hidden) are skipped. Mirrors
 * `enterprise_sim.worlds.file.discover_worlds`.
 */
export function discoverWorlds(root: string, maxDepth = 3): WorldSummary[] {
  const out: WorldSummary[] = []
  const walk = (dir: string, depth: number): void => {
    if (isWorldDir(dir)) {
      const summary = summarizeWorld(dir)
      if (summary) out.push(summary)
      return
    }
    if (depth >= maxDepth) return
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      return
    }
    for (const name of entries.sort()) {
      if (name.startsWith('.')) continue
      const full = join(dir, name)
      try {
        if (statSync(full).isDirectory()) walk(full, depth + 1)
      } catch {
        /* skip */
      }
    }
  }
  if (existsSync(root)) walk(root, 0)
  return out.sort((a, b) => a.world_dir.localeCompare(b.world_dir))
}
