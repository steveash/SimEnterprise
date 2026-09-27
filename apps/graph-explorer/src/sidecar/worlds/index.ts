// Registers the Worlds view's ops (docs/EXPLORER_RUNS.md §4) on the shared
// feature-op registry (`../ops.js`). One `WorldManager` instance supervises
// every build this sidecar process spawns; op handlers are thin adapters from
// `OpContext` onto its methods. `listWorlds` itself is a core op in index.ts
// (the topbar picker needs it before any feature loads) and reads the same
// `.world` files through `./discover.js`.
import { registerOps, type OpContext } from '../ops.js'
import { WorldManager } from './manager.js'
import type {
  WatchWorldEvent,
  WorldConfigureParams,
  WorldCreateParams,
  WorldEstimateParams,
  WorldExtendParams
} from '../../shared/worlds-protocol.js'

const manager = new WorldManager()

const watchAborts = new Map<string, AbortController>()

registerOps({
  worldCatalog: async (ctx: OpContext) => manager.catalog(ctx.repoRoot, { refresh: Boolean(ctx.params.refresh) }),

  worldEstimate: async (ctx: OpContext) => {
    const p = ctx.params as unknown as WorldEstimateParams
    return manager.estimate(ctx.repoRoot, p.config, p.live)
  },

  worldCreate: async (ctx: OpContext) => {
    const p = ctx.params as unknown as WorldCreateParams
    return manager.create(ctx.repoRoot, ctx.worldsRoot, p)
  },

  worldExtend: async (ctx: OpContext) => manager.extend(ctx.repoRoot, ctx.params as unknown as WorldExtendParams),

  worldStart: async (ctx: OpContext) => manager.start(ctx.repoRoot, ctx.params.worldDir as string),

  worldPause: async (ctx: OpContext) => {
    await manager.pause(ctx.repoRoot, ctx.params.worldDir as string)
    return { ok: true }
  },

  worldCancel: async (ctx: OpContext) => {
    await manager.cancel(ctx.repoRoot, ctx.params.worldDir as string)
    return { ok: true }
  },

  worldDelete: async (ctx: OpContext) => {
    await manager.delete(ctx.params.worldDir as string)
    return { ok: true }
  },

  worldStatus: async (ctx: OpContext) => manager.summaryOf(ctx.params.worldDir as string),

  readWorldConfig: async (ctx: OpContext) => manager.readConfig(ctx.params.worldDir as string),

  worldConfigure: async (ctx: OpContext) =>
    manager.configure(ctx.repoRoot, ctx.params as unknown as WorldConfigureParams),

  watchWorld: async (ctx: OpContext) => {
    const worldDir = ctx.params.worldDir as string
    const ac = new AbortController()
    watchAborts.set(ctx.requestId, ac)
    try {
      await manager.watch(worldDir, (e: WatchWorldEvent) => ctx.stream(e), ac.signal)
    } finally {
      watchAborts.delete(ctx.requestId)
    }
    return { watched: true }
  },

  unwatchWorld: async (ctx: OpContext) => {
    watchAborts.get(ctx.params.id as string)?.abort()
    return { unwatched: true }
  }
})

// Exposed for tests / the Templates feature to bust the catalog cache.
export { manager as worldManager }
