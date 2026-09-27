// End-to-end world lifecycle through the REAL `enterprise-sim world` CLI (the
// fake backend — keyless, deterministic), driven entirely through WorldManager
// (no mocking) in a temp worlds root: create -> estimate -> start -> watch
// (live) -> pause -> resume -> done -> listed -> extended in place. Skips
// cleanly (exit 0) when `uv`/`enterprise-sim` cannot be spawned.
import { execFileSync } from 'node:child_process'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { WorldManager } from '../src/sidecar/worlds/manager.js'
import type { JobState, ProgressEvent, WatchWorldEvent, WorldCreateResult } from '../src/shared/worlds-protocol.js'

const APP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const REPO_ROOT = join(APP_ROOT, '..', '..')

function canSpawnPython(): boolean {
  try {
    execFileSync('uv', ['run', '--project', REPO_ROOT, 'enterprise-sim', 'world', 'catalog'], {
      cwd: REPO_ROOT,
      stdio: 'ignore',
      timeout: 30_000
    })
    return true
  } catch {
    return false
  }
}

const PYTHON_AVAILABLE = canSpawnPython()
const TEST_TIMEOUT = 60_000

describe.skipIf(!PYTHON_AVAILABLE)('worlds e2e: create -> estimate -> start -> pause -> resume -> done', () => {
  let worldsRoot: string
  const manager = new WorldManager()

  beforeAll(async () => {
    worldsRoot = await mkdtemp(join(tmpdir(), 'worlds-e2e-'))
  })

  afterAll(async () => {
    await rm(worldsRoot, { recursive: true, force: true })
  })

  const config = {
    company: { name: 'E2E Test Co', vertical: 'software', size: 'startup' },
    simulation: { period_start: '2026-01-05', period_end: '2026-01-16' },
    seed: 3,
    projects: [{ name: 'Alpha', description: 'a' }],
    // max_concurrency=1 makes the render loop strictly sequential, so a pause
    // requested right after the first artifact reliably lands before the
    // second (mirrors tests/test_jobs_pause_resume.py on the Python side).
    scale: { max_concurrency: 1 }
  }

  it(
    'estimates a config with the D13 dry-run gate',
    async () => {
      const estimate = (await manager.estimate(REPO_ROOT, config)) as { num_artifacts: number }
      expect(estimate.num_artifacts).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  let worldDir: string

  it(
    'creates a world directory with a .world (job created) and no scattered state files',
    async () => {
      const created = (await manager.create(REPO_ROOT, worldsRoot, { config, live: false })) as WorldCreateResult
      expect(created.worldId).toBeTruthy()
      worldDir = created.worldDir
      expect(worldDir.startsWith(worldsRoot)).toBe(true)
      expect(existsSync(join(worldDir, '.world'))).toBe(true)
      for (const stale of ['manifest.json', 'config.snapshot.json', 'state.json', 'job.json']) {
        expect(existsSync(join(worldDir, stale))).toBe(false)
      }
      expect(manager.statusOf(worldDir)?.status).toBe('created')
      expect(manager.summaryOf(worldDir)?.has_kg).toBe(false)
    },
    TEST_TIMEOUT
  )

  it(
    'starts the build, watches it live, and pauses it mid-render',
    async () => {
      manager.start(REPO_ROOT, worldDir)

      const events: WatchWorldEvent[] = []
      let artifactCount = 0
      let pauseRequested = false
      await manager.watch(
        worldDir,
        (e) => {
          events.push(e)
          if (e.kind === 'progress' && e.event.kind === 'artifact') {
            artifactCount++
            // Request a pause right after the first artifact lands (a direct
            // .jobs/control.json write, not manager.pause()'s subprocess, so
            // there's no spawn latency racing the next sequential render).
            if (artifactCount === 1 && !pauseRequested) {
              pauseRequested = true
              void mkdir(join(worldDir, '.jobs'), { recursive: true }).then(() =>
                writeFile(join(worldDir, '.jobs', 'control.json'), JSON.stringify({ pause: true }))
              )
            }
          }
        },
        new AbortController().signal
      )

      const last = events[events.length - 1]
      expect(last.kind).toBe('state')
      const state = (last as { state: JobState | null }).state
      expect(state?.status).toBe('paused')
      expect(pauseRequested).toBe(true)
      expect(state?.artifacts_done).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  it(
    'resumes the paused build and lets it run to done, reusing cached artifacts',
    async () => {
      expect(manager.statusOf(worldDir)?.status).toBe('paused')

      manager.start(REPO_ROOT, worldDir) // resume

      const progress: ProgressEvent[] = []
      await manager.watch(
        worldDir,
        (e) => {
          if (e.kind === 'progress') progress.push(e.event)
        },
        new AbortController().signal
      )

      const summary = manager.summaryOf(worldDir)
      expect(summary?.status).toBe('done')
      expect(summary?.has_kg).toBe(true)
      expect(summary?.job.artifacts_done).toBe(summary?.job.artifacts_total)
      expect(summary?.job.segments.map((s) => s.reason)).toEqual(['paused', 'done'])

      // At least one artifact on resume comes back as a cache hit (§2/§3.5's
      // resume guarantee): what the paused segment already rendered is free.
      const artifactEvents = progress.filter((e) => e.kind === 'artifact')
      expect(artifactEvents.some((e) => e.data.cached === true)).toBe(true)
    },
    TEST_TIMEOUT
  )

  it(
    'listing the worlds root includes this world as done, not alive, with its graph',
    async () => {
      const { worlds } = manager.list(worldsRoot)
      const mine = worlds.find((w) => w.world_dir === worldDir)
      expect(mine?.status).toBe('done')
      expect(mine?.alive).toBe(false)
      expect(mine?.has_kg).toBe(true)
      expect(mine?.counts.nodes).toBeGreaterThan(0)
      expect(mine?.history[0].kind).toBe('create')
    },
    TEST_TIMEOUT
  )

  it(
    'extends the world in place: same dir, a later window end, and a history entry',
    async () => {
      const extended = await manager.extend(REPO_ROOT, { worldDir, periodEnd: '2026-01-23' })
      expect(extended.worldDir).toBe(worldDir)
      expect(manager.statusOf(worldDir)?.status).toBe('created')
      const cfg = manager.readConfig(worldDir)
      expect((cfg.config?.simulation as { period_end: string }).period_end).toBe('2026-01-23')
      expect(cfg.history.map((h) => h.kind)).toEqual(['create', 'extend'])
      expect(manager.list(worldsRoot).worlds).toHaveLength(1) // still one folder
    },
    TEST_TIMEOUT
  )
})
