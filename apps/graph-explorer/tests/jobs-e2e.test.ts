// End-to-end job lifecycle through the REAL `enterprise-sim job` CLI (the fake
// backend — keyless, deterministic), driven entirely through JobManager (no
// mocking) in a temp runs root: create -> estimate -> start -> watch (live) ->
// pause -> resume -> done. Skips cleanly (exit 0) when `uv`/`enterprise-sim`
// cannot be spawned, per docs/EXPLORER_RUNS.md §6.
import { execFileSync } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { JobManager } from '../src/sidecar/jobs/manager.js'
import type { JobCreateResult, JobState, ProgressEvent, WatchJobEvent } from '../src/shared/jobs-protocol.js'

const APP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const REPO_ROOT = join(APP_ROOT, '..', '..')

function canSpawnPython(): boolean {
  try {
    execFileSync('uv', ['run', '--project', REPO_ROOT, 'enterprise-sim', 'job', 'catalog'], {
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

describe.skipIf(!PYTHON_AVAILABLE)('jobs e2e: create -> estimate -> start -> pause -> resume -> done', () => {
  let runsRoot: string
  let jobsRoot: string
  const manager = new JobManager()

  beforeAll(async () => {
    runsRoot = await mkdtemp(join(tmpdir(), 'jobs-e2e-'))
    jobsRoot = join(runsRoot, '.jobs')
  })

  afterAll(async () => {
    await rm(runsRoot, { recursive: true, force: true })
  })

  const config = {
    company: { name: 'E2E Test Co', vertical: 'software', size: 'startup' },
    simulation: { period_start: '2026-01-05', period_end: '2026-01-16' },
    seed: 3,
    projects: [{ name: 'Alpha', description: 'a' }],
    // max_concurrency=1 makes the render loop strictly sequential, so a pause
    // requested right after the first artifact reliably lands before the
    // second (mirrors tests/test_jobs_pause_resume.py's approach on the
    // Python side, §6: "use max_concurrency = 1 in that test").
    scale: { max_concurrency: 1 }
  }

  it(
    'estimates a config with the D13 dry-run gate',
    async () => {
      const cfg = { ...config, output_dir: join(runsRoot, 'out') }
      const estimate = (await manager.estimate(REPO_ROOT, jobsRoot, cfg)) as { num_artifacts: number }
      expect(estimate.num_artifacts).toBeGreaterThan(0)
    },
    TEST_TIMEOUT
  )

  let jobDir: string

  it(
    'creates a job directory',
    async () => {
      const cfg = { ...config, output_dir: join(runsRoot, 'out') }
      const created = (await manager.create(REPO_ROOT, jobsRoot, { config: cfg, live: false })) as JobCreateResult
      expect(created.jobId).toBeTruthy()
      jobDir = created.jobDir
      const state = await manager.statusOf(jobDir)
      expect(state?.status).toBe('created')
    },
    TEST_TIMEOUT
  )

  it(
    'starts the job, watches it live, and pauses it mid-render',
    async () => {
      manager.start(REPO_ROOT, jobDir)

      const events: WatchJobEvent[] = []
      const ac = new AbortController()
      let artifactCount = 0
      let pauseRequested = false
      const watchPromise = manager.watch(
        jobDir,
        (e) => {
          events.push(e)
          if (e.kind === 'progress' && e.event.kind === 'artifact') {
            artifactCount++
            // Request a pause right after the first artifact lands, mid-render
            // (a direct control.json write — not manager.pause()'s `job pause`
            // subprocess — so there's no process-spawn latency racing the
            // next, concurrency=1, sequential render).
            if (artifactCount === 1 && !pauseRequested) {
              pauseRequested = true
              void writeFile(join(jobDir, 'control.json'), JSON.stringify({ pause: true }))
            }
          }
        },
        ac.signal
      )

      await watchPromise // resolves once the process exits (paused) and emits the final state
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
    'resumes the paused job and lets it run to done, reusing cached artifacts',
    async () => {
      const beforeState = await manager.statusOf(jobDir)
      expect(beforeState?.status).toBe('paused')

      manager.start(REPO_ROOT, jobDir) // resume

      const progress: ProgressEvent[] = []
      const ac = new AbortController()
      await manager.watch(
        jobDir,
        (e) => {
          if (e.kind === 'progress') progress.push(e.event)
        },
        ac.signal
      )

      const state = await manager.statusOf(jobDir)
      expect(state?.status).toBe('done')
      expect(state?.run_dir).toBeTruthy()
      expect(state?.artifacts_done).toBe(state?.artifacts_total)

      // At least one artifact on resume comes back as a cache hit (§2/§3.5's
      // resume guarantee): what the paused segment already rendered is free.
      const artifactEvents = progress.filter((e) => e.kind === 'artifact')
      expect(artifactEvents.some((e) => e.data.cached === true)).toBe(true)
    },
    TEST_TIMEOUT
  )

  it(
    'listing the jobs root includes this job as done, not alive',
    async () => {
      const { jobs } = await manager.list(jobsRoot)
      const mine = jobs.find((j) => j.job_dir === jobDir)
      expect(mine?.state.status).toBe('done')
      expect(mine?.alive).toBe(false)
    },
    TEST_TIMEOUT
  )
})
