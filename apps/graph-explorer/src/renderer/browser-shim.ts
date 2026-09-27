/**
 * Browser-mode shim for `window.explorer` (dev only; NOT used by the Electron app).
 *
 * The renderer does all its real work over the sidecar WebSocket and only touches
 * Electron for three things: the sidecar port, a native directory picker, and lens
 * persistence. This supplies browser equivalents so the same UI can be served by
 * plain Vite and viewed over an SSH port-forward — useful when the dev box is
 * headless and the display is somewhere else.
 *
 * Launched by `vite.web.config.ts`, which injects this module before `main.tsx`
 * and stamps the sidecar port into `window.__SIDECAR_PORT__`.
 */
import type { Lens, LensInput } from '../shared/lenses.js'

const LENS_KEY = 'graph-explorer.lenses'

const readLenses = (): Lens[] => {
  try {
    const raw = localStorage.getItem(LENS_KEY)
    return raw ? (JSON.parse(raw) as Lens[]) : []
  } catch {
    return [] // corrupt/blocked storage degrades to "no lenses", never a crash
  }
}

const writeLenses = (lenses: Lens[]): Lens[] => {
  try {
    localStorage.setItem(LENS_KEY, JSON.stringify(lenses))
  } catch {
    /* private mode / blocked storage: lenses just don't persist */
  }
  return lenses
}

window.explorer = {
  sidecarInfo: async () => ({ port: window.__SIDECAR_PORT__, worldsRoot: '', error: null }),
  // No native dialog in a browser. Worlds under the worlds root are auto-discovered
  // and listed in the topbar, so this is only for a world outside that root.
  pickWorldDir: async () => window.prompt('Absolute path to a world directory (contains a .world file)') || null,
  lenses: {
    list: async () => readLenses(),
    save: async (input: LensInput) => {
      const existing = readLenses()
      // LensInput carries an id only when overwriting; otherwise mint one.
      const lens: Lens = {
        id: input.id ?? `l${Date.now()}`,
        name: input.name,
        engine: input.engine,
        query: input.query,
        createdAt: existing.find((l) => l.id === input.id)?.createdAt ?? Date.now()
      }
      return writeLenses([...existing.filter((l) => l.id !== lens.id), lens])
    },
    delete: async (id: string) => writeLenses(readLenses().filter((l) => l.id !== id))
  }
}
