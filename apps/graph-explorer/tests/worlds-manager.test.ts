// WorldManager + discovery unit tests (docs/EXPLORER_RUNS.md §4/§6): world
// discovery + summaries straight off `.world` files, JSONL tailing + reattach
// replay against a fake `.jobs/progress.jsonl`, config read from the `.world`,
// and the argv each `world …` subcommand is spawned with. `runJson`/`streamJsonl`
// are mocked here so these tests never spawn Python — the real spawned-process
// path is exercised end-to-end in tests/worlds-e2e.test.ts (which skips
// cleanly without `uv`).
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { JobState, ProgressEvent, WatchWorldEvent } from '../src/shared/worlds-protocol.js'

const runJsonMock = vi.fn()
const streamJsonlMock = vi.fn()

vi.mock('../src/sidecar/python.js', () => ({
  runJson: (...args: unknown[]) => runJsonMock(...args),
  streamJsonl: (...args: unknown[]) => streamJsonlMock(...args)
}))

const { WorldManager, isAlive } = await import('../src/sidecar/worlds/manager.js')
const { discoverWorlds, summarizeWorld, isWorldDir } = await import('../src/sidecar/worlds/discover.js')

let root: string

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'worlds-manager-test-'))
  runJsonMock.mockReset()
  streamJsonlMock.mockReset()
})

afterEach(async () => {
  await rm(root, { recursive: true, force: true })
})

function jobState(overrides: Partial<JobState> = {}): JobState {
  return {
    status: 'running',
    pid: process.pid,
    phase: 'render',
    artifacts_done: 2,
    artifacts_total: 4,
    artifacts_cached: 0,
    estimate: null,
    cost_usd_total: 0.2,
    cost_usd_segment: 0.2,
    usage_total: {},
    segments: [],
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    finished_at: null,
    error: null,
    ...overrides
  }
}

/** Write a minimal `.world` the way enterprise_sim.worlds does. */
async function writeWorld(
  rel: string,
  job: JobState,
  extra: Record<string, unknown> = {},
  opts: { kg?: boolean } = {}
): Promise<string> {
  const dir = join(root, rel)
  await mkdir(dir, { recursive: true })
  const doc = {
    schema_version: '2.0',
    world_id: rel.split('/').pop(),
    company: { name: 'Acme', vertical: 'software', size: 'small' },
    seed: 7,
    window: { start: '2026-01-01', end: '2026-01-31' },
    config: { company: { name: 'Acme' }, seed: 7 },
    config_digest: 'abc',
    render_backend: 'fake',
    live: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    counts: { nodes: 10, edges: 20, events: 5 },
    validation: {},
    outputs: {},
    history: [{ kind: 'create', at: '2026-01-01T00:00:00Z' }],
    lineage: null,
    job,
    data: {},
    evals: {},
    ...extra
  }
  await writeFile(join(dir, '.world'), JSON.stringify(doc))
  if (opts.kg) {
    await mkdir(join(dir, 'kg'), { recursive: true })
    await writeFile(join(dir, 'kg', 'nodes.jsonl'), '')
  }
  return dir
}

describe('isAlive', () => {
  it("is true for this process's own pid", () => {
    expect(isAlive(process.pid)).toBe(true)
  })
  it('is false for null and for an implausible pid', () => {
    expect(isAlive(null)).toBe(false)
    expect(isAlive(999_999_999)).toBe(false)
  })
})

describe('discoverWorlds / summarizeWorld', () => {
  it('lists every .world under the root, derives interrupted from a dead pid, and reports has_kg', async () => {
    await writeWorld('alive-co', jobState({ status: 'running', pid: process.pid }))
    await writeWorld('dead-co', jobState({ status: 'running', pid: 999_999_999 }))
    await writeWorld('group/done-co', jobState({ status: 'done', pid: null }), {}, { kg: true })
    // A world's insides are never worlds, and hidden dirs are skipped.
    await writeWorld('group/done-co/data/trap', jobState())
    await writeWorld('.hidden/x', jobState())

    const worlds = discoverWorlds(root)
    expect(worlds.map((w) => w.world_id)).toEqual(['alive-co', 'dead-co', 'done-co'])
    const byId = Object.fromEntries(worlds.map((w) => [w.world_id, w]))
    expect(byId['alive-co'].status).toBe('running')
    expect(byId['alive-co'].alive).toBe(true)
    expect(byId['dead-co'].status).toBe('interrupted')
    expect(byId['dead-co'].alive).toBe(false)
    expect(byId['done-co'].status).toBe('done')
    expect(byId['done-co'].has_kg).toBe(true)
    expect(byId['alive-co'].has_kg).toBe(false)
    expect(byId['done-co'].counts).toEqual({ nodes: 10, edges: 20, events: 5 })
    expect(byId['done-co'].world_dir).toBe(join(root, 'group', 'done-co'))
    expect(isWorldDir(byId['done-co'].world_dir)).toBe(true)
  })

  it('summarizes structured-data scenarios the way summary.py does', async () => {
    const dir = await writeWorld('data-co', jobState({ status: 'done' }), {
      data: {
        sales_pipeline: {
          title: 'Sales',
          rows_by_table: { dim_rep: 10, fact_opportunity: 2500 },
          questions: { total: 26, answerable: 26 },
          iterations: [{ iteration: 1 }],
          llm_cost_usd: 0.07,
          created_at: '2026-01-02T00:00:00Z'
        }
      },
      evals: { questions: 12 }
    })
    const summary = summarizeWorld(dir)
    expect(summary?.data.sales_pipeline).toEqual({
      title: 'Sales',
      rows: 2510,
      tables: 2,
      questions: { total: 26, answerable: 26 },
      iterations: 1,
      llm_cost_usd: 0.07,
      created_at: '2026-01-02T00:00:00Z'
    })
    expect(summary?.evals.questions).toBe(12)
  })

  it('an empty / missing root lists no worlds, and a non-world dir summarizes to null', async () => {
    expect(discoverWorlds(join(root, 'does-not-exist'))).toEqual([])
    expect(summarizeWorld(root)).toBeNull()
    const manager = new WorldManager()
    expect(manager.list(join(root, 'nope'))).toEqual({ worlds: [] })
    expect(manager.statusOf(join(root, 'nope'))).toBeNull()
  })
})

describe('WorldManager.watch: JSONL tailing + reattach replay', () => {
  it('replays a fake .jobs/progress.jsonl and ends immediately when the world is not being built here', async () => {
    const dir = await writeWorld('w1', jobState({ status: 'paused' }))
    const events: ProgressEvent[] = [
      { kind: 'phase', ts: 1, data: { phase: 'world' } },
      { kind: 'scheduled', ts: 2, data: { artifacts_total: 2 } },
      { kind: 'artifact', ts: 3, data: { done: 1, total: 2, cached: false } }
    ]
    await mkdir(join(dir, '.jobs'))
    await writeFile(join(dir, '.jobs', 'progress.jsonl'), events.map((e) => JSON.stringify(e)).join('\n') + '\n')

    const manager = new WorldManager()
    const seen: WatchWorldEvent[] = []
    await manager.watch(dir, (e) => seen.push(e), new AbortController().signal)

    const progress = seen.filter((e) => e.kind === 'progress')
    expect(progress).toHaveLength(3)
    expect((progress[0] as { event: ProgressEvent }).event.kind).toBe('phase')
    expect((progress[2] as { event: ProgressEvent }).event.data.done).toBe(1)
    // not supervised -> resolves immediately with the current on-disk ledger
    const last = seen[seen.length - 1]
    expect(last.kind).toBe('state')
    expect((last as { state: JobState | null }).state?.status).toBe('paused')
  })

  it('skips malformed lines in progress.jsonl without failing the replay', async () => {
    const dir = await writeWorld('w1', jobState({ status: 'failed' }))
    await mkdir(join(dir, '.jobs'))
    await writeFile(join(dir, '.jobs', 'progress.jsonl'), '{"kind":"phase","ts":1,"data":{}}\nnot json\n\n')
    const manager = new WorldManager()
    const seen: WatchWorldEvent[] = []
    await manager.watch(dir, (e) => seen.push(e), new AbortController().signal)
    expect(seen.filter((e) => e.kind === 'progress')).toHaveLength(1)
  })

  it('an aborted watch on a live-supervised build stops without emitting a final state', async () => {
    let onLine: (obj: unknown) => void = () => {}
    let doneResolve: (code: number | null) => void = () => {}
    const done = new Promise<number | null>((res) => {
      doneResolve = res
    })
    streamJsonlMock.mockImplementation((_repo: string, _args: string[], cb: (o: unknown) => void) => {
      onLine = cb
      return { pid: 4242, done, kill: vi.fn() }
    })

    const dir = await writeWorld('w1', jobState({ status: 'running' }))
    const manager = new WorldManager()
    manager.start('/repo', dir)
    expect(streamJsonlMock.mock.calls[0][1]).toEqual(['world', 'run', dir])
    onLine({ kind: 'phase', ts: 1, data: { phase: 'render' } })

    const seen: WatchWorldEvent[] = []
    const ac = new AbortController()
    const watchPromise = manager.watch(dir, (e) => seen.push(e), ac.signal)
    ac.abort()
    await watchPromise
    expect(seen.some((e) => e.kind === 'state')).toBe(false)

    doneResolve(0)
  })

  it('a still-live watcher receives events streamed after it attaches, then the terminal ledger', async () => {
    let onLine: (obj: unknown) => void = () => {}
    let doneResolve: (code: number | null) => void = () => {}
    const done = new Promise<number | null>((res) => {
      doneResolve = res
    })
    streamJsonlMock.mockImplementation((_repo: string, _args: string[], cb: (o: unknown) => void) => {
      onLine = cb
      return { pid: 4242, done, kill: vi.fn() }
    })

    const dir = await writeWorld('w1', jobState({ status: 'running' }))
    const manager = new WorldManager()
    manager.start('/repo', dir)

    const seen: WatchWorldEvent[] = []
    const watchPromise = manager.watch(dir, (e) => seen.push(e), new AbortController().signal)

    onLine({ kind: 'artifact', ts: 2, data: { done: 1, total: 2 } })
    await new Promise((r) => setTimeout(r, 0))

    await writeWorld('w1', jobState({ status: 'done' }))
    doneResolve(0)
    await watchPromise

    expect(seen.filter((e) => e.kind === 'progress').length).toBeGreaterThanOrEqual(1)
    const last = seen[seen.length - 1]
    expect(last.kind).toBe('state')
    expect((last as { state: JobState | null }).state?.status).toBe('done')
  })
})

describe('WorldManager: readConfig / configure', () => {
  it('reads config + lineage + history off the .world (absent lineage -> null)', async () => {
    const dir = await writeWorld('w1', jobState({ status: 'done' }), {
      config: { company: { name: 'Acme' } },
      history: [{ kind: 'create', at: 't0' }, { kind: 'extend', at: 't1', changes: { period_end: ['a', 'b'] } }]
    })
    const manager = new WorldManager()
    const res = manager.readConfig(dir)
    expect(res.config).toEqual({ company: { name: 'Acme' } })
    expect(res.lineage).toBeNull()
    expect(res.history.map((h) => h.kind)).toEqual(['create', 'extend'])
  })

  it('reads lineage when present', async () => {
    const dir = await writeWorld('w2', jobState(), { lineage: { parent_world_id: 'p1' } })
    const manager = new WorldManager()
    expect(manager.readConfig(dir).lineage).toEqual({ parent_world_id: 'p1' })
  })

  it('configure() spawns `world configure DIR --patch JSON` and returns the config Python printed', async () => {
    let sawArgs: string[] = []
    runJsonMock.mockImplementation(async (_repo: string, args: string[]) => {
      sawArgs = args
      return { world_id: 'w', config: { scale: { cost_ceiling_usd: 50 } } }
    })
    const manager = new WorldManager()
    const { config } = await manager.configure('/repo', { worldDir: '/w', patch: { scale: { cost_ceiling_usd: 50 } } })
    expect(config).toEqual({ scale: { cost_ceiling_usd: 50 } })
    expect(sawArgs).toEqual(['world', 'configure', '/w', '--patch', JSON.stringify({ scale: { cost_ceiling_usd: 50 } })])
  })
})

describe('WorldManager: catalog caching', () => {
  it('caches world+templates catalog per repoRoot until refresh/invalidate', async () => {
    runJsonMock.mockImplementation((_repo: string, args: string[]) => {
      if (args[0] === 'world') return Promise.resolve({ backends: ['fake'] })
      return Promise.resolve({ archetypes: [], playbooks: [], processes: [] })
    })
    const manager = new WorldManager()
    await manager.catalog('/repo')
    await manager.catalog('/repo')
    expect(runJsonMock).toHaveBeenCalledTimes(2) // one call per subcommand, not per catalog() call
    expect(runJsonMock.mock.calls[0][1]).toEqual(['world', 'catalog'])

    await manager.catalog('/repo', { refresh: true })
    expect(runJsonMock).toHaveBeenCalledTimes(4)

    manager.invalidateCatalog('/repo')
    await manager.catalog('/repo')
    expect(runJsonMock).toHaveBeenCalledTimes(6)
  })
})

describe('WorldManager: create/extend/estimate spawn the right `world …` argv via mocked python.js', () => {
  it('estimate() writes a temp config file (cleaned up after) and calls `world estimate --config <path>`', async () => {
    let sawConfigPath = ''
    let sawConfigContent: unknown = null
    let sawArgs: string[] = []
    runJsonMock.mockImplementation(async (_repo: string, args: string[]) => {
      sawArgs = args
      const idx = args.indexOf('--config')
      sawConfigPath = args[idx + 1]
      sawConfigContent = JSON.parse(await readFile(sawConfigPath, 'utf-8'))
      return { num_artifacts: 1 }
    })
    const manager = new WorldManager()
    const result = await manager.estimate('/repo', { company: { name: 'Acme' } }, true)
    expect(result).toEqual({ num_artifacts: 1 })
    expect(sawArgs.slice(0, 2)).toEqual(['world', 'estimate'])
    expect(sawArgs).toContain('--live')
    expect(sawConfigContent).toEqual({ company: { name: 'Acme' } })
    await expect(readFile(sawConfigPath, 'utf-8')).rejects.toThrow()
  })

  it('create() mints without building (`--no-run`) under the worlds root and forwards --live', async () => {
    let sawArgs: string[] = []
    runJsonMock.mockImplementation(async (_repo: string, args: string[]) => {
      sawArgs = args
      return { world_id: 'acme-1', world_dir: '/worlds/acme-1' }
    })
    const manager = new WorldManager()
    const created = await manager.create('/repo', '/worlds', { config: { company: { name: 'Acme' } }, live: true })
    expect(created).toEqual({ worldId: 'acme-1', worldDir: '/worlds/acme-1' })
    expect(sawArgs.slice(0, 2)).toEqual(['world', 'create'])
    expect(sawArgs).toContain('--worlds-root')
    expect(sawArgs).toContain('/worlds')
    expect(sawArgs).toContain('--no-run')
    expect(sawArgs).toContain('--live')
  })

  it('extend() forwards --period-end and repeated --add-project against the SAME world dir', async () => {
    let sawArgs: string[] = []
    runJsonMock.mockImplementation(async (_repo: string, args: string[]) => {
      sawArgs = args
      return { world_id: 'acme-1', world_dir: '/worlds/acme-1', changes: { added_projects: ['Beta'] } }
    })
    const manager = new WorldManager()
    const res = await manager.extend('/repo', {
      worldDir: '/worlds/acme-1',
      periodEnd: '2026-02-01',
      addProjects: [{ name: 'Beta' }]
    })
    expect(res.worldDir).toBe('/worlds/acme-1')
    expect(res.changes).toEqual({ added_projects: ['Beta'] })
    expect(sawArgs.slice(0, 3)).toEqual(['world', 'extend', '/worlds/acme-1'])
    expect(sawArgs).toContain('--no-run')
    expect(sawArgs).toContain('--period-end')
    expect(sawArgs).toContain('2026-02-01')
    expect(sawArgs).toContain('--add-project')
    expect(sawArgs).toContain(JSON.stringify({ name: 'Beta' }))
    expect(sawArgs).not.toContain('--live')
  })
})
