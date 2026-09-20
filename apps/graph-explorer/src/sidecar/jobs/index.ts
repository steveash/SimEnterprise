// Registers the Runs view's ops (docs/EXPLORER_RUNS.md §4) on the shared
// feature-op registry (`../ops.js`). One `JobManager` instance supervises
// every job this sidecar process spawns; op handlers are thin adapters from
// `OpContext` onto its methods.
import { join } from 'node:path'
import { registerOps, type OpContext } from '../ops.js'
import { JobManager } from './manager.js'
import type {
  JobCreateParams,
  JobEstimateParams,
  JobUpdateConfigParams,
  WatchJobEvent
} from '../../shared/jobs-protocol.js'

const manager = new JobManager()

function jobsRootOf(ctx: OpContext): string {
  return join(ctx.runsRoot, '.jobs')
}

const watchAborts = new Map<string, AbortController>()

registerOps({
  jobCatalog: async (ctx: OpContext) => manager.catalog(ctx.repoRoot, { refresh: Boolean(ctx.params.refresh) }),

  jobEstimate: async (ctx: OpContext) => {
    const p = ctx.params as unknown as JobEstimateParams
    return manager.estimate(ctx.repoRoot, jobsRootOf(ctx), p.config, p.live)
  },

  jobCreate: async (ctx: OpContext) => {
    const p = ctx.params as unknown as JobCreateParams
    return manager.create(ctx.repoRoot, jobsRootOf(ctx), p)
  },

  jobStart: async (ctx: OpContext) => manager.start(ctx.repoRoot, ctx.params.jobDir as string),

  jobPause: async (ctx: OpContext) => {
    await manager.pause(ctx.repoRoot, ctx.params.jobDir as string)
    return { ok: true }
  },

  jobCancel: async (ctx: OpContext) => {
    await manager.cancel(ctx.repoRoot, ctx.params.jobDir as string)
    return { ok: true }
  },

  jobDelete: async (ctx: OpContext) => {
    await manager.delete(ctx.params.jobDir as string)
    return { ok: true }
  },

  jobList: async (ctx: OpContext) => manager.list(jobsRootOf(ctx)),

  jobStatus: async (ctx: OpContext) => manager.statusOf(ctx.params.jobDir as string),

  readRunConfig: async (ctx: OpContext) => manager.readRunConfig(ctx.params.runPath as string),

  jobUpdateConfig: async (ctx: OpContext) => manager.updateConfig(ctx.params as unknown as JobUpdateConfigParams),

  watchJob: async (ctx: OpContext) => {
    const jobDir = ctx.params.jobDir as string
    const ac = new AbortController()
    watchAborts.set(ctx.requestId, ac)
    try {
      await manager.watch(jobDir, (e: WatchJobEvent) => ctx.stream(e), ac.signal)
    } finally {
      watchAborts.delete(ctx.requestId)
    }
    return { watched: true }
  },

  unwatchJob: async (ctx: OpContext) => {
    watchAborts.get(ctx.params.id as string)?.abort()
    return { unwatched: true }
  }
})

// Exposed for tests / a future Templates-feature hook to bust the catalog cache.
export { manager as jobManager }
