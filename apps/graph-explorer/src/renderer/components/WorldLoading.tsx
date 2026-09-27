// World-loading feedback. Loading a world builds its in-memory index and both
// query engines (Kùzu for Cypher, Oxigraph + inference for SPARQL), which takes
// several seconds on a real world — so every entry point (the topbar picker,
// "Open in Explore", the Open… dialog) shows the same thing: an indeterminate
// bar along the topbar with the world's name and elapsed time, and an overlay
// on the Explore canvas that sits above the (stale) graph.
import { useEffect, useState } from 'react'
import { useStore } from '../store.js'

function useElapsed(since: number | null): number {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (since === null) return
    const id = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(id)
  }, [since])
  return since === null ? 0 : Math.max(0, (now - since) / 1000)
}

function useLoadingName(): string | null {
  const path = useStore((s) => s.loadingWorldPath)
  const worlds = useStore((s) => s.worlds)
  if (!path) return null
  return worlds.find((w) => w.world_dir === path)?.world_id ?? path.split('/').pop() ?? path
}

/** Topbar chip + an indeterminate progress bar along the bottom edge of the topbar. */
export function WorldLoading(): JSX.Element | null {
  const since = useStore((s) => s.loadingStartedAt)
  const name = useLoadingName()
  const elapsed = useElapsed(since)
  if (!name) return null
  return (
    <>
      <span className="world-loading-chip" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" />
        Loading <span className="mono">{name}</span> · {elapsed.toFixed(0)}s
      </span>
      <div className="topbar-progress" aria-hidden="true" />
    </>
  )
}

/** Overlay on the Explore canvas while a world loads (above the previous world's graph). */
export function WorldLoadingOverlay(): JSX.Element | null {
  const since = useStore((s) => s.loadingStartedAt)
  const name = useLoadingName()
  const elapsed = useElapsed(since)
  if (!name) return null
  const stage =
    elapsed < 2 ? 'reading the knowledge graph…' : elapsed < 6 ? 'building the Cypher engine…' : 'materializing SPARQL inferences…'
  return (
    <div className="world-loading-overlay" role="status" aria-live="polite">
      <div className="world-loading-card">
        <span className="spinner lg" aria-hidden="true" />
        <div className="world-loading-title">
          Loading <span className="mono">{name}</span>
        </div>
        <div className="muted small">
          {stage} {elapsed.toFixed(0)}s
        </div>
        <div className="world-loading-bar" aria-hidden="true" />
      </div>
    </div>
  )
}
