// JobManager unit tests (docs/EXPLORER_RUNS.md §4/§6): JSONL tailing + reattach
// replay against a fake `progress.jsonl`, listing/status straight off disk, and
// config read/patch. `runJson`/`streamJsonl` are mocked here so these tests
// never spawn Python — the real spawned-process path is exercised end-to-end
// in tests/jobs-e2e.test.ts (which skips cleanly without `uv`).
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { JobState, ProgressEvent, WatchJobEvent } from '../src/shared/jobs-protocol.js'

const runJsonMock = vi.fn()
const streamJsonlMock = vi.fn()

vi.mock('../src/sidecar/python.js', () => ({
  runJson: (...args: unknown[]) => runJsonMock(...args),
  streamJsonl: (...args: unknown[]) => streamJsonlMock(...args)
}))

const { JobManager, isAlive } = await import('../src/sidecar/jobs/manager.js')

let root: string

beforeEach(async () => {
  root = await mkdtemp(join(tmpdir(), 'jobs-manager-test-'))
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
    run_id: null,
    run_dir: null,
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

async function writeJobDir(jobId: string, state: JobState, job: Record<string, unknown> = {}): Promise<string> {
  const jobDir = join(root, jobId)
  await mkdir(jobDir, { recursive: true })
  await writeFile(join(jobDir, 'state.json'), JSON.stringify(state))
  await writeFile(join(jobDir, 'job.json'), JSON.stringify({ kind: 'new', live: false, created_at: 'x', ...job }))
  return jobDir
}

describe('isAlive', () => {
  it('is true for this process\'s own pid', () => {
    expect(isAlive(process.pid)).toBe(true)
  })
  it('is false for null and for an implausible pid', () => {
    expect(isAlive(null)).toBe(false)
    expect(isAlive(999_999_999)).toBe(false)
  })
})

describe('JobManager.list / statusOf', () => {
  it('reads every state.json under the jobs root and computes alive via process.kill(pid, 0)', async () => {
    await writeJobDir('20260101-000000-alive-co', jobState({ status: 'running', pid: process.pid }))
    await writeJobDir('20260101-000001-dead-co', jobState({ status: 'running', pid: 999_999_999 }))
    await writeJobDir('20260101-000002-done-co', jobState({ status: 'done', pid: null, run_dir: '/runs/x' }))

    const manager = new JobManager()
    const { jobs } = await manager.list(root)
    expect(jobs).toHaveLength(3)
    const byId = Object.fromEntries(jobs.map((j) => [j.job_id, j]))
    expect(byId['20260101-000000-alive-co'].alive).toBe(true)
    expect(byId['20260101-000001-dead-co'].alive).toBe(false)
    // a finished job is never reported "alive" (status isn't "running")
    expect(byId['20260101-000002-done-co'].alive).toBe(false)
  })

  it('an empty / missing jobs root lists no jobs', async () => {
    const manager = new JobManager()
    expect(await manager.list(join(root, 'does-not-exist'))).toEqual({ jobs: [] })
  })

  it('statusOf reads state.json directly, or null if absent', async () => {
    const jobDir = await writeJobDir('j1', jobState({ status: 'paused' }))
    const manager = new JobManager()
    expect((await manager.statusOf(jobDir))?.status).toBe('paused')
    expect(await manager.statusOf(join(root, 'nope'))).toBeNull()
  })
})

describe('JobManager.watch: JSONL tailing + reattach replay', () => {
  it('replays a fake progress.jsonl and ends immediately when the job is not supervised', async () => {
    const jobDir = await writeJobDir('j1', jobState({ status: 'paused' }))
    const events: ProgressEvent[] = [
      { kind: 'phase', ts: 1, data: { phase: 'world' } },
      { kind: 'scheduled', ts: 2, data: { artifacts_total: 2 } },
      { kind: 'artifact', ts: 3, data: { done: 1, total: 2, cached: false } }
    ]
    await writeFile(join(jobDir, 'progress.jsonl'), events.map((e) => JSON.stringify(e)).join('\n') + '\n')

    const manager = new JobManager()
    const seen: WatchJobEvent[] = []
    const ac = new AbortController()
    await manager.watch(jobDir, (e) => seen.push(e), ac.signal)

    const progress = seen.filter((e) => e.kind === 'progress')
    expect(progress).toHaveLength(3)
    expect((progress[0] as { event: ProgressEvent }).event.kind).toBe('phase')
    expect((progress[2] as { event: ProgressEvent }).event.data.done).toBe(1)
    // not supervised -> resolves immediately with the current on-disk state
    const last = seen[seen.length - 1]
    expect(last.kind).toBe('state')
    expect((last as { state: JobState | null }).state?.status).toBe('paused')
  })

  it('skips malformed lines in progress.jsonl without failing the replay', async () => {
    const jobDir = await writeJobDir('j1', jobState({ status: 'failed' }))
    await writeFile(join(jobDir, 'progress.jsonl'), '{"kind":"phase","ts":1,"data":{}}\nnot json\n\n')
    const manager = new JobManager()
    const seen: WatchJobEvent[] = []
    await manager.watch(jobDir, (e) => seen.push(e), new AbortController().signal)
    expect(seen.filter((e) => e.kind === 'progress')).toHaveLength(1)
  })

  it('an aborted watch on a live-supervised job stops without emitting a final state', async () => {
    // Simulate an active supervision by starting a job whose child process
    // never exits within the test, then abort the watch.
    let onLine: (obj: unknown) => void = () => {}
    let doneResolve: (code: number | null) => void = () => {}
    const done = new Promise<number | null>((res) => {
      doneResolve = res
    })
    streamJsonlMock.mockImplementation((_repo: string, _args: string[], cb: (o: unknown) => void) => {
      onLine = cb
      return { pid: 4242, done, kill: vi.fn() }
    })

    const jobDir = await writeJobDir('j1', jobState({ status: 'running' }))
    const manager = new JobManager()
    manager.start('/repo', jobDir)
    onLine({ kind: 'phase', ts: 1, data: { phase: 'render' } })

    const seen: WatchJobEvent[] = []
    const ac = new AbortController()
    const watchPromise = manager.watch(jobDir, (e) => seen.push(e), ac.signal)
    ac.abort()
    await watchPromise
    expect(seen.some((e) => e.kind === 'state')).toBe(false)

    doneResolve(0) // let the (unused) supervision settle so nothing leaks between tests
  })

  it('a still-live watcher receives events streamed after it attaches, then the terminal state', async () => {
    let onLine: (obj: unknown) => void = () => {}
    let doneResolve: (code: number | null) => void = () => {}
    const done = new Promise<number | null>((res) => {
      doneResolve = res
    })
    streamJsonlMock.mockImplementation((_repo: string, _args: string[], cb: (o: unknown) => void) => {
      onLine = cb
      return { pid: 4242, done, kill: vi.fn() }
    })

    const jobDir = await writeJobDir('j1', jobState({ status: 'running' }))
    const manager = new JobManager()
    manager.start('/repo', jobDir)

    const seen: WatchJobEvent[] = []
    const watchPromise = manager.watch(jobDir, (e) => seen.push(e), new AbortController().signal)

    onLine({ kind: 'artifact', ts: 2, data: { done: 1, total: 2 } })
    // let the microtask queue drain so the listener registered by watch() runs
    await new Promise((r) => setTimeout(r, 0))

    await writeFile(join(jobDir, 'state.json'), JSON.stringify(jobState({ status: 'done', run_dir: '/runs/x' })))
    doneResolve(0)
    await watchPromise

    const progress = seen.filter((e) => e.kind === 'progress')
    expect(progress.length).toBeGreaterThanOrEqual(1)
    const last = seen[seen.length - 1]
    expect(last.kind).toBe('state')
    expect((last as { state: JobState | null }).state?.status).toBe('done')
  })
})

describe('JobManager: readRunConfig / updateConfig', () => {
  it('reads config.snapshot.json and lineage.json (absent lineage -> null)', async () => {
    const runDir = join(root, 'run1')
    await mkdir(runDir, { recursive: true })
    await writeFile(join(runDir, 'config.snapshot.json'), JSON.stringify({ company: { name: 'Acme' } }))
    const manager = new JobManager()
    const res = await manager.readRunConfig(runDir)
    expect(res.config).toEqual({ company: { name: 'Acme' } })
    expect(res.lineage).toBeNull()
  })

  it('reads lineage.json when present', async () => {
    const runDir = join(root, 'run2')
    await mkdir(runDir, { recursive: true })
    await writeFile(join(runDir, 'config.snapshot.json'), JSON.stringify({}))
    await writeFile(join(runDir, 'lineage.json'), JSON.stringify({ parent_run_id: 'p1' }))
    const manager = new JobManager()
    const res = await manager.readRunConfig(runDir)
    expect(res.lineage).toEqual({ parent_run_id: 'p1' })
  })

  it('updateConfig deep-merges a patch onto config.json (e.g. raising the cost ceiling)', async () => {
    const jobDir = join(root, 'job1')
    await mkdir(jobDir, { recursive: true })
    await writeFile(
      join(jobDir, 'config.json'),
      JSON.stringify({ scale: { cost_ceiling_usd: 5, max_concurrency: 8 }, seed: 3 })
    )
    const manager = new JobManager()
    const { config } = await manager.updateConfig({ jobDir, patch: { scale: { cost_ceiling_usd: 50 } } })
    expect(config.scale).toEqual({ cost_ceiling_usd: 50, max_concurrency: 8 })
    expect(config.seed).toBe(3)

    const onDisk = JSON.parse(await readFile(join(jobDir, 'config.json'), 'utf-8'))
    expect(onDisk.scale.cost_ceiling_usd).toBe(50)
  })
})

describe('JobManager: catalog caching', () => {
  it('caches job+templates catalog per repoRoot until refresh/invalidate', async () => {
    runJsonMock.mockImplementation((_repo: string, args: string[]) => {
      if (args[0] === 'job') return Promise.resolve({ backends: ['fake'] })
      return Promise.resolve({ archetypes: [], playbooks: [], processes: [] })
    })
    const manager = new JobManager()
    await manager.catalog('/repo')
    await manager.catalog('/repo')
    expect(runJsonMock).toHaveBeenCalledTimes(2) // one call per subcommand, not per catalog() call

    await manager.catalog('/repo', { refresh: true })
    expect(runJsonMock).toHaveBeenCalledTimes(4)

    manager.invalidateCatalog('/repo')
    await manager.catalog('/repo')
    expect(runJsonMock).toHaveBeenCalledTimes(6)
  })
})

describe('JobManager: create/estimate build a config JSON, spawn via mocked python.js', () => {
  it('estimate() writes a temp config file (cleaned up after) and calls `job estimate --config <path>`', async () => {
    let sawConfigPath = ''
    let sawConfigContent: unknown = null
    runJsonMock.mockImplementation(async (_repo: string, args: string[]) => {
      const idx = args.indexOf('--config')
      sawConfigPath = args[idx + 1]
      // Read it here, while `withTempConfig`'s callback is still running — the
      // file is deleted in its `finally` right after this resolves.
      sawConfigContent = JSON.parse(await readFile(sawConfigPath, 'utf-8'))
      return { num_artifacts: 1 }
    })
    const manager = new JobManager()
    const result = await manager.estimate('/repo', join(root, '.jobs'), { company: { name: 'Acme' } })
    expect(result).toEqual({ num_artifacts: 1 })
    expect(sawConfigPath).toContain(join(root, '.jobs', '.tmp'))
    expect(sawConfigContent).toEqual({ company: { name: 'Acme' } })
    // cleaned up: the path no longer exists once estimate() has returned
    await expect(readFile(sawConfigPath, 'utf-8')).rejects.toThrow()
  })

  it('create() forwards --live and repeated --add-project for an extend request', async () => {
    let sawArgs: string[] = []
    runJsonMock.mockImplementation(async (_repo: string, args: string[]) => {
      sawArgs = args
      return { job_id: 'j', job_dir: '/jobs/j' }
    })
    const manager = new JobManager()
    const jobsRoot = join(root, '.jobs')
    await manager.create('/repo', jobsRoot, {
      config: { company: { name: 'Acme' } },
      live: true,
      extend: { parentRunDir: '/runs/parent', periodEnd: '2026-02-01', addProjects: [{ name: 'Beta' }] }
    })
    expect(sawArgs).toContain('--live')
    expect(sawArgs).toContain('--extend')
    expect(sawArgs).toContain('/runs/parent')
    expect(sawArgs).toContain('--period-end')
    expect(sawArgs).toContain('2026-02-01')
    expect(sawArgs).toContain('--add-project')
    expect(sawArgs).toContain(JSON.stringify({ name: 'Beta' }))
  })
})
